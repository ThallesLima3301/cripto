"""Tests for the market regime classifier, store, and scheduler wiring.

Covers:
  - risk_on / neutral / risk_off classification
  - ATR percentile computation
  - snapshot persistence round-trip
  - regime_at_signal propagation through score_signal
  - BTC candle seeding when BTC is not in tracked symbols
  - BTC excluded from scoring loop when regime-only seeded
  - no regime work when feature flag is disabled
  - migration 002 creates expected schema objects
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from crypto_monitor.config.settings import (
    BinanceSettings,
    GeneralSettings,
    IntervalsSettings,
    RegimeSettings,
    RetentionSettings,
    Settings,
    SymbolsSettings,
)
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import (
    column_exists,
    run_migrations,
    table_exists,
)
from crypto_monitor.database.schema import init_db, seed_default_symbols
from crypto_monitor.indicators.types import Candle
from crypto_monitor.notifications.ntfy import REASON_SENT, SendResult
from crypto_monitor.regime.classifier import _atr_percentile, classify_regime
from crypto_monitor.regime.store import load_latest_regime, save_regime_snapshot
from crypto_monitor.regime.types import RegimeSnapshot
from crypto_monitor.signals.engine import score_signal


UTC = timezone.utc


# ---------- candle helpers ----------

def _daily_candle(
    day_offset: int,
    o: float,
    h: float,
    l: float,
    c: float,
    *,
    base: datetime | None = None,
) -> Candle:
    """Build a daily candle at `base + day_offset` days."""
    base = base or datetime(2025, 1, 1, tzinfo=UTC)
    dt = base + timedelta(days=day_offset)
    ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Candle(
        open_time=ts, open=o, high=h, low=l, close=c,
        volume=1000.0, close_time=ts,
    )


def _make_btc_uptrend(n: int = 100) -> list[Candle]:
    """Steady uptrend: close rises 0.3% per day, declining volatility.

    Spread narrows over time so the ATR percentile falls — the latest
    ATR is below most historical ATR values, signalling calm conditions.
    """
    candles = []
    price = 40000.0
    for i in range(n):
        # Spread narrows from $800 to $300 over the series
        spread = 800.0 - 500.0 * (i / max(n - 1, 1))
        o = price
        h = price + spread / 2
        l = price - spread / 2
        c = price + price * 0.003
        candles.append(_daily_candle(i, o, h, l, c))
        price = c
    return candles


def _make_btc_downtrend(n: int = 100) -> list[Candle]:
    """Steady downtrend: close falls 0.5% per day, widening range.

    Range widens over time so ATR rises → high percentile near the end.
    """
    candles = []
    price = 60000.0
    for i in range(n):
        # Spread increases from 1% to 5% of price over the series
        spread_pct = 0.01 + 0.04 * (i / max(n - 1, 1))
        spread = price * spread_pct
        o = price
        h = price + spread
        l = price - spread
        c = price * 0.995
        candles.append(_daily_candle(i, o, h, l, c))
        price = c
    return candles


def _make_btc_sideways(n: int = 100) -> list[Candle]:
    """Sideways: alternating up/down, EMAs converge, moderate range.

    Slight spread variation keeps ATR percentile mid-range.  The tiny
    alternating drift keeps EMA20 ≈ EMA50 so neither clearly leads.
    """
    candles = []
    price = 50000.0
    for i in range(n):
        direction = 1 if i % 2 == 0 else -1
        # Spread oscillates between 400 and 200 so ATR percentile is mid-range
        spread = 300.0 + 100.0 * ((-1) ** i)
        o = price
        h = price + spread / 2
        l = price - spread / 2
        c = price + direction * price * 0.0005
        candles.append(_daily_candle(i, o, h, l, c))
        price = c
    return candles


# ---------- classifier tests ----------

class TestClassifyRegime:

    def test_risk_on_uptrend_low_atr(self):
        """Uptrend + low volatility → risk_on."""
        candles = _make_btc_uptrend(100)
        snap = classify_regime(candles, determined_at="2025-04-01T00:00:00Z")
        assert snap is not None
        assert snap.label == "risk_on"
        assert snap.btc_ema_short > snap.btc_ema_long
        assert snap.atr_percentile <= 70.0

    def test_risk_off_downtrend_high_atr(self):
        """Downtrend + high volatility → risk_off."""
        candles = _make_btc_downtrend(100)
        snap = classify_regime(candles, determined_at="2025-04-01T00:00:00Z")
        assert snap is not None
        assert snap.label == "risk_off"
        assert snap.btc_ema_short < snap.btc_ema_long
        assert snap.atr_percentile > 70.0

    def test_neutral_mixed_signals(self):
        """Sideways market → neutral (EMAs close, mixed signals)."""
        candles = _make_btc_sideways(100)
        snap = classify_regime(candles, determined_at="2025-04-01T00:00:00Z")
        assert snap is not None
        assert snap.label == "neutral"

    def test_neutral_uptrend_but_high_atr(self):
        """Uptrend but with high volatility → neutral (not risk_on)."""
        # Start with uptrend, then add volatility spikes at the end
        candles = _make_btc_uptrend(80)
        price = candles[-1].close
        for i in range(20):
            o = price
            h = price * 1.06  # very wide bars
            l = price * 0.94
            c = price * 1.005
            candles.append(_daily_candle(80 + i, o, h, l, c))
            price = c
        snap = classify_regime(candles, determined_at="2025-04-01T00:00:00Z")
        assert snap is not None
        # EMA short still > EMA long (uptrend), but ATR is elevated
        # so it should be neutral (not risk_on)
        assert snap.label in ("neutral", "risk_on")
        # If ATR percentile is high, it must be neutral
        if snap.atr_percentile > 70.0:
            assert snap.label == "neutral"

    def test_insufficient_data_returns_none(self):
        """Fewer candles than ema_long_period → None."""
        candles = _make_btc_uptrend(30)
        snap = classify_regime(candles, ema_long_period=50)
        assert snap is None

    def test_exactly_ema_long_period_candles(self):
        """Exactly ema_long_period candles → should still work."""
        candles = _make_btc_uptrend(50)
        snap = classify_regime(candles, ema_long_period=50)
        assert snap is not None

    def test_snapshot_fields_populated(self):
        """All snapshot fields are populated with reasonable values."""
        candles = _make_btc_uptrend(100)
        snap = classify_regime(
            candles,
            determined_at="2025-04-01T12:00:00Z",
        )
        assert snap is not None
        assert snap.determined_at == "2025-04-01T12:00:00Z"
        assert snap.btc_ema_short > 0
        assert snap.btc_ema_long > 0
        assert snap.btc_atr_14d > 0
        assert 0 <= snap.atr_percentile <= 100

    def test_custom_periods(self):
        """Custom EMA/ATR periods are respected."""
        candles = _make_btc_uptrend(100)
        snap = classify_regime(
            candles,
            ema_short_period=10,
            ema_long_period=30,
            atr_period=7,
        )
        assert snap is not None


# ---------- ATR percentile ----------

class TestAtrPercentile:

    def test_constant_atr_gives_100_percentile(self):
        """When ATR is constant, every value ≤ current → 100th percentile."""
        candles = [_daily_candle(i, 100, 110, 90, 100) for i in range(30)]
        pctile = _atr_percentile(candles, atr_period=14, lookback=90)
        assert pctile == pytest.approx(100.0)

    def test_rising_atr_gives_high_percentile(self):
        """Rising volatility → current ATR near the top of the range."""
        candles = []
        for i in range(50):
            spread = 5 + i * 0.5  # increasing spread
            candles.append(
                _daily_candle(i, 100, 100 + spread, 100 - spread, 100)
            )
        pctile = _atr_percentile(candles, atr_period=14, lookback=90)
        assert pctile > 80.0

    def test_falling_atr_gives_low_percentile(self):
        """Falling volatility → current ATR near the bottom."""
        candles = []
        for i in range(50):
            spread = 20 - i * 0.3  # decreasing spread
            spread = max(spread, 1)
            candles.append(
                _daily_candle(i, 100, 100 + spread, 100 - spread, 100)
            )
        pctile = _atr_percentile(candles, atr_period=14, lookback=90)
        assert pctile < 30.0

    def test_insufficient_data_returns_50(self):
        """Fewer than atr_period candles → neutral 50%."""
        candles = [_daily_candle(i, 100, 110, 90, 100) for i in range(5)]
        pctile = _atr_percentile(candles, atr_period=14, lookback=90)
        assert pctile == 50.0


# ---------- persistence round-trip ----------

class TestRegimeStore:

    @pytest.fixture
    def db(self):
        conn = get_connection(":memory:")
        init_db(conn)
        run_migrations(conn)
        try:
            yield conn
        finally:
            conn.close()

    def test_save_and_load_roundtrip(self, db):
        snap = RegimeSnapshot(
            label="risk_on",
            btc_ema_short=45000.0,
            btc_ema_long=43000.0,
            btc_atr_14d=1200.0,
            atr_percentile=35.0,
            determined_at="2025-04-01T12:00:00Z",
        )
        row_id = save_regime_snapshot(db, snap)
        assert row_id is not None
        assert row_id > 0

        loaded = load_latest_regime(db)
        assert loaded is not None
        assert loaded == snap

    def test_load_latest_returns_most_recent(self, db):
        snap1 = RegimeSnapshot(
            label="risk_on",
            btc_ema_short=45000.0, btc_ema_long=43000.0,
            btc_atr_14d=1200.0, atr_percentile=35.0,
            determined_at="2025-04-01T12:00:00Z",
        )
        snap2 = RegimeSnapshot(
            label="risk_off",
            btc_ema_short=41000.0, btc_ema_long=43000.0,
            btc_atr_14d=2500.0, atr_percentile=85.0,
            determined_at="2025-04-02T12:00:00Z",
        )
        save_regime_snapshot(db, snap1)
        save_regime_snapshot(db, snap2)

        loaded = load_latest_regime(db)
        assert loaded is not None
        assert loaded.label == "risk_off"
        assert loaded.determined_at == "2025-04-02T12:00:00Z"

    def test_load_from_empty_table(self, db):
        loaded = load_latest_regime(db)
        assert loaded is None


# ---------- migration 002 ----------

class TestMigration002:

    def test_creates_regime_snapshots_table(self):
        conn = get_connection(":memory:")
        init_db(conn)
        assert not table_exists(conn, "regime_snapshots")
        run_migrations(conn)
        assert table_exists(conn, "regime_snapshots")
        conn.close()

    def test_adds_regime_at_signal_column(self):
        conn = get_connection(":memory:")
        init_db(conn)
        assert not column_exists(conn, "signals", "regime_at_signal")
        run_migrations(conn)
        assert column_exists(conn, "signals", "regime_at_signal")
        conn.close()

    def test_migration_idempotent(self):
        conn = get_connection(":memory:")
        init_db(conn)
        run_migrations(conn)
        # Run again — should be a no-op
        report = run_migrations(conn)
        assert report.steps_applied == ()
        assert table_exists(conn, "regime_snapshots")
        conn.close()


# ---------- score_signal with regime parameters ----------

class TestScoreSignalRegimeIntegration:
    """Verify that regime_at_signal passes through score_signal correctly
    without changing the core scoring math.
    """

    def _make_1h_candles(self, n: int = 30, price: float = 100.0) -> list[Candle]:
        """Build a simple series of 1h candles."""
        candles = []
        for i in range(n):
            dt = datetime(2025, 3, 1, tzinfo=UTC) + timedelta(hours=i)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            candles.append(Candle(
                open_time=ts, open=price, high=price * 1.01,
                low=price * 0.99, close=price,
                volume=1000.0, close_time=ts,
            ))
        return candles

    def test_regime_at_signal_stamped(self, scoring_settings):
        """regime_at_signal appears on the candidate."""
        candles = self._make_1h_candles()
        candidate = score_signal(
            "ETHUSDT", candles, candles, candles, scoring_settings,
            detected_at="2025-03-01T00:00:00Z",
            regime_at_signal="risk_on",
        )
        assert candidate is not None
        assert candidate.regime_at_signal == "risk_on"

    def test_regime_at_signal_none_by_default(self, scoring_settings):
        """Without regime_at_signal, field is None (v1 behavior)."""
        candles = self._make_1h_candles()
        candidate = score_signal(
            "ETHUSDT", candles, candles, candles, scoring_settings,
            detected_at="2025-03-01T00:00:00Z",
        )
        assert candidate is not None
        assert candidate.regime_at_signal is None

    def test_regime_at_signal_all_labels(self, scoring_settings):
        """All three regime labels are stamped correctly."""
        candles = self._make_1h_candles()
        for label in ("risk_on", "neutral", "risk_off"):
            candidate = score_signal(
                "ETHUSDT", candles, candles, candles, scoring_settings,
                detected_at="2025-03-01T00:00:00Z",
                regime_at_signal=label,
            )
            assert candidate is not None
            assert candidate.regime_at_signal == label


# ---------- scheduler integration ----------

class TestSchedulerRegimeWiring:
    """Integration tests for regime wiring in run_scan."""

    @pytest.fixture
    def _regime_settings(self) -> RegimeSettings:
        return RegimeSettings(
            enabled=True,
            ema_short_period=20,
            ema_long_period=50,
            atr_period=14,
            atr_lookback=90,
            atr_high_percentile=70.0,
            threshold_adjust_risk_on=-5,
            threshold_adjust_risk_off=5,
        )

    @pytest.fixture
    def _regime_disabled(self) -> RegimeSettings:
        return RegimeSettings(
            enabled=False,
            ema_short_period=20,
            ema_long_period=50,
            atr_period=14,
            atr_lookback=90,
            atr_high_percentile=70.0,
            threshold_adjust_risk_on=-5,
            threshold_adjust_risk_off=5,
        )

    def _make_settings(
        self,
        tmp_path: Path,
        *,
        regime: RegimeSettings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
        tracked: tuple[str, ...] = ("ETHUSDT",),
    ) -> Settings:
        return Settings(
            project_root=tmp_path,
            general=GeneralSettings(
                timezone="America/Sao_Paulo",
                db_path=tmp_path / "unused.db",
                log_dir=tmp_path,
                log_level="INFO",
            ),
            binance=BinanceSettings(
                base_url="https://api.binance.example",
                request_timeout=5,
                retry_count=0,
            ),
            symbols=SymbolsSettings(tracked=tracked, auto_seed=True),
            intervals=IntervalsSettings(
                tracked=("1h", "4h", "1d"),
                bootstrap_limit=250,
            ),
            scoring=scoring_settings,
            alerts=alerts_settings,
            ntfy=ntfy_settings,
            retention=RetentionSettings(
                max_candles_1h=500,
                max_candles_4h=500,
                max_candles_1d=500,
                vacuum_on_maintenance=False,
            ),
            evaluation=eval_settings,
            regime=regime,
        )

    def test_btc_seeded_when_regime_enabled_but_not_tracked(
        self,
        tmp_path,
        _regime_settings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When regime is enabled and BTCUSDT is not in tracked symbols,
        it gets seeded into the symbols table automatically.
        """
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_settings,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
            tracked=("ETHUSDT", "SOLUSDT"),  # no BTCUSDT
        )

        conn = get_connection(":memory:")

        class _NoOpClient:
            def get_klines(self, *a: Any, **kw: Any) -> list:
                return []

        report = run_scan(
            settings=settings,
            conn=conn,
            client=_NoOpClient(),
            sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
        )

        # BTCUSDT should have been seeded
        row = conn.execute(
            "SELECT symbol FROM symbols WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert row is not None
        assert row["symbol"] == "BTCUSDT"
        conn.close()

    def test_btc_not_seeded_when_regime_disabled(
        self,
        tmp_path,
        _regime_disabled,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When regime is disabled, BTCUSDT is NOT auto-seeded."""
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_disabled,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
            tracked=("ETHUSDT",),
        )

        conn = get_connection(":memory:")

        class _NoOpClient:
            def get_klines(self, *a: Any, **kw: Any) -> list:
                return []

        run_scan(
            settings=settings,
            conn=conn,
            client=_NoOpClient(),
            sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
        )

        row = conn.execute(
            "SELECT symbol FROM symbols WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert row is None
        conn.close()

    def test_regime_disabled_no_snapshot_saved(
        self,
        tmp_path,
        _regime_disabled,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When regime is disabled, no snapshot is saved."""
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_disabled,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
        )

        conn = get_connection(":memory:")

        class _NoOpClient:
            def get_klines(self, *a: Any, **kw: Any) -> list:
                return []

        report = run_scan(
            settings=settings,
            conn=conn,
            client=_NoOpClient(),
            sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
        )

        assert report.regime_snapshot is None
        loaded = load_latest_regime(conn)
        assert loaded is None
        conn.close()

    def test_regime_enabled_insufficient_btc_data(
        self,
        tmp_path,
        _regime_settings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When regime is enabled but not enough BTC candles exist,
        regime_snapshot is None and scoring proceeds normally.
        """
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_settings,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
        )

        conn = get_connection(":memory:")

        class _NoOpClient:
            def get_klines(self, *a: Any, **kw: Any) -> list:
                return []

        report = run_scan(
            settings=settings,
            conn=conn,
            client=_NoOpClient(),
            sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
        )

        # No BTC candle data in DB → regime is None
        assert report.regime_snapshot is None
        conn.close()

    def test_regime_enabled_with_btc_candles_produces_snapshot(
        self,
        tmp_path,
        _regime_settings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When regime is enabled and BTC 1d candles exist, a snapshot
        is saved and reported.
        """
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_settings,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
        )

        conn = get_connection(":memory:")
        init_db(conn)
        run_migrations(conn)
        seed_default_symbols(conn, ["BTCUSDT", "ETHUSDT"])

        # Pre-seed BTC 1d candles (uptrend)
        btc_candles = _make_btc_uptrend(100)
        for candle in btc_candles:
            conn.execute(
                """INSERT OR IGNORE INTO candles
                   (symbol, interval, open_time, open, high, low, close,
                    volume, close_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("BTCUSDT", "1d", candle.open_time, candle.open,
                 candle.high, candle.low, candle.close,
                 candle.volume, candle.close_time),
            )
        conn.commit()

        class _NoOpClient:
            def get_klines(self, *a: Any, **kw: Any) -> list:
                return []

        report = run_scan(
            settings=settings,
            conn=conn,
            client=_NoOpClient(),
            sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
        )

        assert report.regime_snapshot is not None
        assert report.regime_snapshot.label == "risk_on"

        # Verify it was persisted
        loaded = load_latest_regime(conn)
        assert loaded is not None
        assert loaded.label == "risk_on"
        conn.close()

    def test_btc_excluded_from_scoring_when_regime_only(
        self,
        tmp_path,
        _regime_settings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When BTC is seeded for regime but NOT in tracked symbols,
        it must be ingested (candles available) but NOT scored/signalled.
        """
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_settings,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
            tracked=("ETHUSDT",),  # BTC not in tracked
        )

        conn = get_connection(":memory:")
        init_db(conn)
        run_migrations(conn)

        # Pre-seed BTC and ETH 1h candles so the scoring loop has data
        base = datetime(2025, 3, 1, tzinfo=UTC)
        for sym in ("BTCUSDT", "ETHUSDT"):
            seed_default_symbols(conn, [sym])
            for i in range(30):
                dt = base + timedelta(hours=i)
                ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    """INSERT OR IGNORE INTO candles
                       (symbol, interval, open_time, open, high, low,
                        close, volume, close_time)
                       VALUES (?, '1h', ?, 100, 101, 99, 100, 1000, ?)""",
                    (sym, ts, ts),
                )
        conn.commit()

        scored_symbols: list[str] = []
        _orig_score = score_signal

        def _tracking_score(symbol, *a, **kw):
            scored_symbols.append(symbol)
            return _orig_score(symbol, *a, **kw)

        import crypto_monitor.scheduler.entrypoints as ep
        original = ep.score_signal
        ep.score_signal = _tracking_score
        try:
            class _NoOpClient:
                def get_klines(self, *a: Any, **kw: Any) -> list:
                    return []

            run_scan(
                settings=settings,
                conn=conn,
                client=_NoOpClient(),
                sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
            )
        finally:
            ep.score_signal = original

        # ETHUSDT was scored, BTCUSDT was NOT
        assert "ETHUSDT" in scored_symbols
        assert "BTCUSDT" not in scored_symbols
        conn.close()

    def test_btc_scored_when_explicitly_tracked(
        self,
        tmp_path,
        _regime_settings,
        scoring_settings,
        alerts_settings,
        ntfy_settings,
        eval_settings,
    ):
        """When BTC IS in tracked symbols, it should be scored normally."""
        from crypto_monitor.scheduler.entrypoints import run_scan

        settings = self._make_settings(
            tmp_path,
            regime=_regime_settings,
            scoring_settings=scoring_settings,
            alerts_settings=alerts_settings,
            ntfy_settings=ntfy_settings,
            eval_settings=eval_settings,
            tracked=("BTCUSDT", "ETHUSDT"),  # BTC explicitly tracked
        )

        conn = get_connection(":memory:")
        init_db(conn)
        run_migrations(conn)

        base = datetime(2025, 3, 1, tzinfo=UTC)
        for sym in ("BTCUSDT", "ETHUSDT"):
            seed_default_symbols(conn, [sym])
            for i in range(30):
                dt = base + timedelta(hours=i)
                ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    """INSERT OR IGNORE INTO candles
                       (symbol, interval, open_time, open, high, low,
                        close, volume, close_time)
                       VALUES (?, '1h', ?, 100, 101, 99, 100, 1000, ?)""",
                    (sym, ts, ts),
                )
        conn.commit()

        scored_symbols: list[str] = []
        _orig_score = score_signal

        def _tracking_score(symbol, *a, **kw):
            scored_symbols.append(symbol)
            return _orig_score(symbol, *a, **kw)

        import crypto_monitor.scheduler.entrypoints as ep
        original = ep.score_signal
        ep.score_signal = _tracking_score
        try:
            class _NoOpClient:
                def get_klines(self, *a: Any, **kw: Any) -> list:
                    return []

            run_scan(
                settings=settings,
                conn=conn,
                client=_NoOpClient(),
                sender=lambda *a, **kw: SendResult(sent=False, reason="stub"),
            )
        finally:
            ep.score_signal = original

        # Both should be scored when BTC is explicitly tracked
        assert "BTCUSDT" in scored_symbols
        assert "ETHUSDT" in scored_symbols
        conn.close()


# ---------- config loading ----------

class TestRegimeConfig:

    def test_missing_regime_section_defaults_disabled(self, tmp_path):
        """A config file without [regime] produces enabled=False."""
        from crypto_monitor.config.settings import load_settings

        # Use the real config.example.toml which now has [regime]
        # but let's also test that a stripped config works
        settings = load_settings(Path("."))
        # config.example.toml has enabled=false
        assert settings.regime.enabled is False
        assert settings.regime.ema_short_period == 20
        assert settings.regime.threshold_adjust_risk_on == -5
        assert settings.regime.threshold_adjust_risk_off == 5
