"""Block 21 — sell-side runtime orchestration.

Covers ``crypto_monitor.sell.runtime.process_open_positions`` and its
integration into ``run_scan``:

  * settings.enabled gate
  * full per-buy path: evaluate -> insert -> notify -> watermark update
  * cooldown suppression uses per-(buy, rule) lookup
  * watermark ordering: trailing-stop sees the PRIOR watermark
  * sold buys are excluded
  * send failure keeps ``alerted = 0``
  * ``run_scan`` wires the sell pass when enabled

No network is touched — ntfy is always stubbed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from crypto_monitor.buys.manual import insert_buy
from crypto_monitor.config.settings import (
    BinanceSettings,
    GeneralSettings,
    IntervalsSettings,
    NtfySettings,
    RegimeSettings,
    RetentionSettings,
    SellSettings,
    Settings,
    SymbolsSettings,
    WatchlistSettings,
)
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db, seed_default_symbols
from crypto_monitor.notifications.ntfy import (
    REASON_NETWORK_ERROR,
    REASON_SENT,
    SendResult,
)
from crypto_monitor.scheduler import run_scan
from crypto_monitor.sell import (
    get_high_watermark,
    insert_sell_signal,
    process_open_positions,
    record_sale,
)
from crypto_monitor.sell.types import SellSignal


UTC = timezone.utc
NOW = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)


# ---------- fixtures ----------

@pytest.fixture
def db():
    conn = get_connection(":memory:")
    init_db(conn)
    run_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class _SenderCall:
    title: str
    body: str
    priority: str
    tags: tuple[str, ...]


class _RecordingSender:
    """Captures every ntfy send call; returns the pre-canned result."""

    def __init__(self, result: SendResult) -> None:
        self._result = result
        self.calls: list[_SenderCall] = []

    def __call__(
        self,
        ntfy: Any,
        title: str,
        body: str,
        *,
        priority: str = "default",
        tags: tuple[str, ...] = (),
        **_: Any,
    ) -> SendResult:
        self.calls.append(
            _SenderCall(title=title, body=body, priority=priority, tags=tags)
        )
        return self._result


def _sell_settings(
    *,
    enabled: bool = True,
    stop_loss_pct: float = 8.0,
    take_profit_pct: float = 20.0,
    trailing_stop_pct: float = 10.0,
    context_deterioration: bool = True,
    cooldown_hours: int = 6,
) -> SellSettings:
    return SellSettings(
        enabled=enabled,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        context_deterioration=context_deterioration,
        cooldown_hours=cooldown_hours,
    )


def _ntfy() -> NtfySettings:
    return NtfySettings(
        server_url="https://ntfy.test",
        topic="t",
        default_tags=("crypto",),
        request_timeout=5,
        max_retries=1,
        debug_notifications=False,
    )


def _constant_price(price: float):
    def _lookup(_conn: sqlite3.Connection, _symbol: str) -> float | None:
        return price
    return _lookup


def _price_by_symbol(mapping: dict[str, float | None]):
    def _lookup(_conn: sqlite3.Connection, symbol: str) -> float | None:
        return mapping.get(symbol)
    return _lookup


def _make_buy(
    db: sqlite3.Connection,
    *,
    symbol: str = "BTCUSDT",
    price: float = 100.0,
    when: datetime = datetime(2026, 4, 20, 0, 0, tzinfo=UTC),
) -> int:
    return insert_buy(
        db,
        symbol=symbol,
        bought_at=when,
        price=price,
        amount_invested=1000.0,
        now=when,
    ).id


# ---------- settings.enabled gate ----------

class TestEnabledGate:

    def test_disabled_short_circuits(self, db):
        buy_id = _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = process_open_positions(
            db,
            settings=_sell_settings(enabled=False),
            ntfy=_ntfy(),
            now=NOW,
            sender=sender,
            price_lookup=_constant_price(85.0),  # would trigger stop_loss
        )
        assert report.considered == 0
        assert report.evaluated == 0
        assert report.signals_emitted == 0
        assert sender.calls == []
        # No watermark either — the pass did nothing.
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) is None


# ---------- happy path ----------

class TestHappyPath:

    def test_stop_loss_fires_inserts_and_notifies(self, db):
        buy_id = _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )

        report = process_open_positions(
            db,
            settings=_sell_settings(),
            ntfy=_ntfy(),
            regime_label="neutral",
            now=NOW,
            sender=sender,
            price_lookup=_constant_price(85.0),  # -15% -> stop_loss
        )

        assert report.considered == 1
        assert report.evaluated == 1
        assert report.signals_emitted == 1
        assert report.signals_sent == 1
        assert report.cooldown_suppressed == 0
        assert report.errors == []

        assert len(sender.calls) == 1
        call = sender.calls[0]
        assert "Stop-loss" in call.title
        assert "BTC" in call.title
        assert "85" in call.body

        # sell_signals row is persisted AND alerted flipped to 1.
        row = db.execute(
            "SELECT symbol, rule_triggered, severity, alerted, "
            "price_at_signal, regime_at_signal FROM sell_signals "
            "WHERE buy_id = ?",
            (buy_id,),
        ).fetchone()
        assert row is not None
        assert row["symbol"] == "BTCUSDT"
        assert row["rule_triggered"] == "stop_loss"
        assert row["severity"] == "high"
        assert row["alerted"] == 1
        assert row["price_at_signal"] == 85.0
        assert row["regime_at_signal"] == "neutral"

    def test_no_rule_fires_silent_run(self, db):
        _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = process_open_positions(
            db,
            settings=_sell_settings(),
            ntfy=_ntfy(),
            now=NOW,
            sender=sender,
            price_lookup=_constant_price(105.0),  # +5%, nothing fires
        )
        assert report.signals_emitted == 0
        assert sender.calls == []

    def test_no_price_available_is_counted(self, db):
        _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = process_open_positions(
            db,
            settings=_sell_settings(),
            ntfy=_ntfy(),
            now=NOW,
            sender=sender,
            price_lookup=lambda c, s: None,
        )
        assert report.considered == 1
        assert report.evaluated == 0
        assert report.no_price == 1
        assert report.signals_emitted == 0


# ---------- cooldown ----------

class TestCooldown:

    def test_cooldown_suppresses_repeat_alert_for_same_rule(self, db):
        buy_id = _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )

        first = process_open_positions(
            db, settings=_sell_settings(cooldown_hours=6), ntfy=_ntfy(),
            now=NOW, sender=sender, price_lookup=_constant_price(85.0),
        )
        assert first.signals_emitted == 1

        later = NOW + timedelta(hours=2)  # inside the 6h cooldown
        second = process_open_positions(
            db, settings=_sell_settings(cooldown_hours=6), ntfy=_ntfy(),
            now=later, sender=sender, price_lookup=_constant_price(82.0),
        )
        assert second.signals_emitted == 0
        assert second.cooldown_suppressed == 1
        # Only one sell row, only one send.
        count = db.execute(
            "SELECT COUNT(*) FROM sell_signals WHERE buy_id = ?",
            (buy_id,),
        ).fetchone()[0]
        assert count == 1
        assert len(sender.calls) == 1

    def test_cooldown_expires_and_next_cycle_fires(self, db):
        buy_id = _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )

        process_open_positions(
            db, settings=_sell_settings(cooldown_hours=6), ntfy=_ntfy(),
            now=NOW, sender=sender, price_lookup=_constant_price(85.0),
        )
        much_later = NOW + timedelta(hours=7)
        second = process_open_positions(
            db, settings=_sell_settings(cooldown_hours=6), ntfy=_ntfy(),
            now=much_later, sender=sender, price_lookup=_constant_price(82.0),
        )
        assert second.signals_emitted == 1
        count = db.execute(
            "SELECT COUNT(*) FROM sell_signals WHERE buy_id = ?",
            (buy_id,),
        ).fetchone()[0]
        assert count == 2

    def test_cooldown_is_per_rule(self, db):
        """A recent stop_loss does NOT silence a subsequent trailing_stop."""
        buy_id = _make_buy(db)
        # Hand-insert a stop_loss row 1 hour ago.
        insert_sell_signal(
            db,
            SellSignal(
                id=None,
                symbol="BTCUSDT",
                buy_id=buy_id,
                detected_at=(NOW - timedelta(hours=1))
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                price_at_signal=90.0,
                rule_triggered="stop_loss",
                severity="high",
                reason="synthetic prior stop_loss",
                pnl_pct=-10.0,
            ),
        )

        # Put a prior watermark high enough that trailing_stop fires now.
        db.execute(
            """INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at)
               VALUES ('BTCUSDT', ?, 140.0, ?)""",
            (buy_id, (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        db.commit()

        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        # Price 125: stop_loss_floor = 92, 125 > 92 (no stop_loss).
        # trail_floor = 140 * 0.9 = 126, 125 <= 126 (trailing_stop fires).
        report = process_open_positions(
            db, settings=_sell_settings(cooldown_hours=6), ntfy=_ntfy(),
            now=NOW, sender=sender, price_lookup=_constant_price(125.0),
        )
        assert report.signals_emitted == 1
        row = db.execute(
            "SELECT rule_triggered FROM sell_signals "
            "WHERE buy_id = ? ORDER BY id DESC LIMIT 1",
            (buy_id,),
        ).fetchone()
        assert row["rule_triggered"] == "trailing_stop"


# ---------- watermark ordering ----------

class TestWatermarkOrdering:

    def test_watermark_initialized_to_max_of_entry_and_price(self, db):
        buy_id = _make_buy(db, price=100.0)
        process_open_positions(
            db, settings=_sell_settings(), ntfy=_ntfy(),
            now=NOW, sender=_RecordingSender(
                SendResult(sent=True, reason=REASON_SENT, status_code=200)
            ),
            price_lookup=_constant_price(105.0),
        )
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 105.0

    def test_watermark_uses_entry_when_price_is_below(self, db):
        """First observation below entry: baseline is buy.price, not price."""
        buy_id = _make_buy(db, price=100.0)
        process_open_positions(
            db, settings=_sell_settings(), ntfy=_ntfy(),
            now=NOW, sender=_RecordingSender(
                SendResult(sent=True, reason=REASON_SENT, status_code=200)
            ),
            price_lookup=_constant_price(95.0),  # below entry, no rule fires
        )
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 100.0

    def test_evaluation_uses_prior_watermark_not_current(self, db):
        """Trailing-stop must see the PRIOR watermark on this cycle.

        Pre-seed hwm=150. Price=130 (−13.3% from 150). Without the
        guarantee, a same-cycle update to hwm=150 first, then an
        evaluate that uses the new value, would still fire (correct),
        but if an implementation updated to hwm=max(150, 130)=150 AFTER
        evaluation and fed the evaluator a post-update number that
        somehow excluded the price we'd see silence. The straightforward
        check: after the cycle, exactly one trailing_stop row exists AND
        the watermark is unchanged at 150 (current 130 is below it).
        """
        buy_id = _make_buy(db, price=100.0)
        db.execute(
            """INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at)
               VALUES ('BTCUSDT', ?, 150.0, '2026-04-20T00:00:00Z')""",
            (buy_id,),
        )
        db.commit()

        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = process_open_positions(
            db, settings=_sell_settings(trailing_stop_pct=10.0),
            ntfy=_ntfy(), now=NOW, sender=sender,
            price_lookup=_constant_price(130.0),  # <= 150*0.9=135 -> trail fires
        )
        assert report.signals_emitted == 1
        # Watermark stays at 150 because current (130) is below it.
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 150.0

    def test_watermark_is_monotone_not_lowered_by_low_price(self, db):
        buy_id = _make_buy(db, price=100.0)
        # Seed hwm=130.
        db.execute(
            """INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at)
               VALUES ('BTCUSDT', ?, 130.0, '2026-04-20T00:00:00Z')""",
            (buy_id,),
        )
        db.commit()
        process_open_positions(
            db, settings=_sell_settings(), ntfy=_ntfy(),
            now=NOW, sender=_RecordingSender(
                SendResult(sent=True, reason=REASON_SENT, status_code=200)
            ),
            price_lookup=_constant_price(115.0),  # below hwm
        )
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 130.0

    def test_watermark_raises_when_price_breaks_prior_high(self, db):
        buy_id = _make_buy(db, price=100.0)
        db.execute(
            """INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at)
               VALUES ('BTCUSDT', ?, 130.0, '2026-04-20T00:00:00Z')""",
            (buy_id,),
        )
        db.commit()
        process_open_positions(
            db, settings=_sell_settings(take_profit_pct=100.0),  # disable TP noise
            ntfy=_ntfy(), now=NOW,
            sender=_RecordingSender(
                SendResult(sent=True, reason=REASON_SENT, status_code=200)
            ),
            price_lookup=_constant_price(145.0),
        )
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 145.0


# ---------- sold-buy exclusion ----------

class TestSoldExclusion:

    def test_sold_buys_are_skipped(self, db):
        sold_id = _make_buy(db, symbol="ETHUSDT", price=100.0)
        open_id = _make_buy(db, symbol="BTCUSDT", price=100.0)
        record_sale(
            db,
            buy_id=sold_id,
            sold_at=NOW - timedelta(days=1),
            sold_price=110.0,
        )

        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = process_open_positions(
            db, settings=_sell_settings(), ntfy=_ntfy(),
            now=NOW, sender=sender,
            price_lookup=_price_by_symbol({"BTCUSDT": 85.0, "ETHUSDT": 85.0}),
        )
        # Only the open buy is considered and evaluated.
        assert report.considered == 1
        assert report.evaluated == 1
        # Exactly one sell signal was inserted, and it's for the open buy.
        rows = db.execute(
            "SELECT buy_id FROM sell_signals"
        ).fetchall()
        assert [r["buy_id"] for r in rows] == [open_id]


# ---------- send failure ----------

class TestSendFailure:

    def test_failed_send_keeps_alerted_zero(self, db):
        _make_buy(db)
        sender = _RecordingSender(
            SendResult(sent=False, reason=REASON_NETWORK_ERROR, error="boom")
        )
        report = process_open_positions(
            db, settings=_sell_settings(), ntfy=_ntfy(),
            now=NOW, sender=sender,
            price_lookup=_constant_price(85.0),
        )
        assert report.signals_emitted == 1
        assert report.signals_sent == 0
        assert report.signals_send_failed == 1
        row = db.execute(
            "SELECT alerted FROM sell_signals"
        ).fetchone()
        assert row["alerted"] == 0


# ---------- scheduler integration ----------

def _make_settings(
    tmp_path: Path,
    *,
    sell: SellSettings,
    tracked: tuple[str, ...] = ("BTCUSDT",),
) -> Settings:
    """Minimal Settings for run_scan. Reuses the conftest-style defaults."""
    from tests.conftest import (  # local import to stay test-scoped
        _mk as _unused_mk,  # noqa: F401
    )
    # Build scoring/alerts/ntfy/eval inline so this test file is self-contained.
    from crypto_monitor.config.settings import (
        ScoringSettings, ScoringWeights, ScoringThresholds, ScoringSeverity,
        AlertSettings, EvaluationSettings,
    )
    scoring = ScoringSettings(
        weights=ScoringWeights(25, 20, 15, 15, 10, 10, 5),
        thresholds=ScoringThresholds(
            min_signal_score=50,
            drop_1h=(1.0,), drop_1h_points=(5,),
            drop_24h=(3.0,), drop_24h_points=(8,),
            drop_7d=(5.0,), drop_7d_points=(5,),
            drop_30d=(15.0,), drop_30d_points=(8,),
            drop_180d=(30.0,), drop_180d_points=(6,),
            rsi_1h_levels=(30.0,), rsi_1h_points=(12,),
            rsi_4h_levels=(35.0,), rsi_4h_points=(5,),
            rel_volume_levels=(1.5,), rel_volume_points=(5,),
            support_distance_levels=(0.5,), support_distance_points=(15,),
            support_lookback_days=90,
            discount_30d_levels=(10.0,), discount_30d_points=(2,),
            discount_180d_levels=(20.0,), discount_180d_points=(2,),
        ),
        severity=ScoringSeverity(normal=50, strong=65, very_strong=80),
    )
    return Settings(
        project_root=tmp_path,
        general=GeneralSettings(
            timezone="UTC", db_path=tmp_path / "x.db",
            log_dir=tmp_path, log_level="INFO",
        ),
        binance=BinanceSettings(
            base_url="https://api.binance.example",
            request_timeout=5, retry_count=0,
        ),
        symbols=SymbolsSettings(tracked=tracked, auto_seed=True),
        intervals=IntervalsSettings(tracked=("1h", "4h", "1d"), bootstrap_limit=250),
        scoring=scoring,
        alerts=AlertSettings(
            cooldown_minutes=120, escalation_jump=10,
            quiet_hours_start=22, quiet_hours_end=8,
        ),
        ntfy=_ntfy(),
        retention=RetentionSettings(
            max_candles_1h=500, max_candles_4h=500, max_candles_1d=500,
            vacuum_on_maintenance=False,
        ),
        evaluation=EvaluationSettings(
            great_return_pct=10.0, good_return_pct=5.0,
            poor_return_pct=-5.0, bad_return_pct=-10.0,
        ),
        regime=RegimeSettings(
            enabled=False,
            ema_short_period=20, ema_long_period=50,
            atr_period=14, atr_lookback=90,
            atr_high_percentile=70.0,
            threshold_adjust_risk_on=-5, threshold_adjust_risk_off=5,
        ),
        sell=sell,
        watchlist=WatchlistSettings(
            enabled=False, floor_score=35, max_watch_hours=48,
        ),
    )


class _NoOpClient:
    def get_klines(self, *a: Any, **kw: Any) -> list:
        return []


class TestSchedulerIntegration:

    def test_run_scan_invokes_sell_pass_when_enabled(self, tmp_path, db):
        settings = _make_settings(tmp_path, sell=_sell_settings())
        seed_default_symbols(db, list(settings.symbols.tracked))
        buy_id = _make_buy(db, price=100.0)
        # Seed one 1h candle so the default price_lookup finds a price
        # below the stop-loss threshold.
        db.execute(
            """INSERT OR IGNORE INTO candles
               (symbol, interval, open_time, open, high, low, close,
                volume, close_time)
               VALUES ('BTCUSDT', '1h', ?, 85, 85, 85, 85, 100, ?)""",
            ("2026-04-23T14:00:00Z", "2026-04-23T14:59:59Z"),
        )
        db.commit()

        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=sender,
        )
        assert report.sell_report is not None
        assert report.sell_report.signals_emitted == 1
        # Buy-signal alerts did NOT fire (flat candles -> no buy signal).
        assert report.process_report is not None
        assert report.process_report.sent == 0
        # Exactly one sell send through the injected sender.
        assert len(sender.calls) == 1
        assert "Stop-loss" in sender.calls[0].title

        row = db.execute(
            "SELECT buy_id FROM sell_signals"
        ).fetchone()
        assert row["buy_id"] == buy_id

    def test_run_scan_skips_sell_pass_when_disabled(self, tmp_path, db):
        settings = _make_settings(tmp_path, sell=_sell_settings(enabled=False))
        seed_default_symbols(db, list(settings.symbols.tracked))
        _make_buy(db, price=100.0)
        db.execute(
            """INSERT OR IGNORE INTO candles
               (symbol, interval, open_time, open, high, low, close,
                volume, close_time)
               VALUES ('BTCUSDT', '1h', ?, 85, 85, 85, 85, 100, ?)""",
            ("2026-04-23T14:00:00Z", "2026-04-23T14:59:59Z"),
        )
        db.commit()
        sender = _RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=sender,
        )
        assert report.sell_report is None
        count = db.execute("SELECT COUNT(*) FROM sell_signals").fetchone()[0]
        assert count == 0
