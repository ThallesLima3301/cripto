"""Block 23 — watchlist integration into the scan loop.

The scoring engine itself is exhaustively tested elsewhere
(``test_signal_engine.py``). Here we monkey-patch
``crypto_monitor.scheduler.entrypoints.score_signal`` to return a
fabricated ``SignalCandidate`` with a chosen ``score`` and
``severity`` so we can drive every state-machine branch
deterministically.

Coverage:
  * borderline score creates a watching entry
  * borderline score on an existing watch refreshes (no duplicate row)
  * a later scan that crosses the emit threshold promotes the watch
    AND links ``signals.watchlist_id``
  * score below floor expires an active watching row
  * score below floor with no watch is ignored (no row written)
  * when watchlist is disabled the scan path is unchanged
  * stale watches are expired once per scan cycle
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from crypto_monitor.config.settings import (
    AlertSettings,
    BinanceSettings,
    EvaluationSettings,
    GeneralSettings,
    IntervalsSettings,
    NtfySettings,
    RegimeSettings,
    RetentionSettings,
    ScoringSeverity,
    ScoringSettings,
    ScoringThresholds,
    ScoringWeights,
    SellSettings,
    Settings,
    SymbolsSettings,
    WatchlistSettings,
)
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db, seed_default_symbols
from crypto_monitor.notifications.ntfy import REASON_SENT, SendResult
from crypto_monitor.scheduler import run_scan
from crypto_monitor.signals.types import SignalCandidate
from crypto_monitor.watchlist import (
    expire_below_floor,
    get_watching,
    list_watching,
    upsert_watching,
)


UTC = timezone.utc
NOW = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
ENT_MOD = sys.modules["crypto_monitor.scheduler.entrypoints"]


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


def _candidate(
    *,
    symbol: str = "BTCUSDT",
    score: int,
    severity: str | None,
    detected_at: str = "2026-04-23T15:00:00Z",
) -> SignalCandidate:
    return SignalCandidate(
        symbol=symbol,
        candle_hour=detected_at,
        detected_at=detected_at,
        price_at_signal=100.0,
        score=score,
        severity=severity,
        drop_1h_pct=None, drop_24h_pct=None,
        drop_7d_pct=None, drop_30d_pct=None, drop_180d_pct=None,
        dominant_trigger_timeframe=None,
        trigger_reason="test",
        drop_trigger_pct=None,
        recent_30d_high=None, recent_180d_high=None,
        distance_from_30d_high_pct=None, distance_from_180d_high_pct=None,
        rsi_1h=None, rsi_4h=None, rel_volume=None,
        dist_support_pct=None, support_level_price=None,
        reversal_signal=False, reversal_pattern=None,
        trend_context_4h="sideways", trend_context_1d="sideways",
        score_breakdown={},
        regime_at_signal=None,
        watchlist_id=None,
    )


def _patch_score(monkeypatch, fn: Callable[..., SignalCandidate | None]) -> None:
    """Replace ``score_signal`` in the scheduler module with ``fn``."""
    monkeypatch.setattr(ENT_MOD, "score_signal", fn)


def _ntfy() -> NtfySettings:
    return NtfySettings(
        server_url="https://ntfy.test", topic="t",
        default_tags=("crypto",),
        request_timeout=5, max_retries=1, debug_notifications=False,
    )


def _settings(
    tmp_path: Path,
    *,
    watchlist_enabled: bool = True,
    floor_score: int = 35,
    max_watch_hours: int = 48,
    tracked: tuple[str, ...] = ("BTCUSDT",),
) -> Settings:
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
        intervals=IntervalsSettings(tracked=("1h",), bootstrap_limit=250),
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
        sell=SellSettings(
            enabled=False, stop_loss_pct=8.0,
            take_profit_pct=20.0, trailing_stop_pct=10.0,
            context_deterioration=True, cooldown_hours=6,
        ),
        watchlist=WatchlistSettings(
            enabled=watchlist_enabled,
            floor_score=floor_score,
            max_watch_hours=max_watch_hours,
        ),
    )


class _NoOpClient:
    def get_klines(self, *a: Any, **kw: Any) -> list:
        return []


@dataclass
class _SenderCall:
    title: str
    body: str
    priority: str
    tags: tuple[str, ...]


class _RecordingSender:
    def __init__(self) -> None:
        self.calls: list[_SenderCall] = []

    def __call__(self, ntfy, title, body, *, priority="default", tags=(), **_):
        self.calls.append(
            _SenderCall(title=title, body=body, priority=priority, tags=tags)
        )
        return SendResult(sent=True, reason=REASON_SENT, status_code=200)


def _seed_one_1h_candle(db: sqlite3.Connection, symbol: str = "BTCUSDT") -> None:
    """Minimum candle so the scoring loop reaches the score_signal call."""
    db.execute(
        """INSERT OR IGNORE INTO candles
           (symbol, interval, open_time, open, high, low, close,
            volume, close_time)
           VALUES (?, '1h', ?, 100, 100, 100, 100, 100, ?)""",
        (symbol, "2026-04-23T14:00:00Z", "2026-04-23T14:59:59Z"),
    )
    db.commit()


# ---------- WATCH path ----------

class TestWatchPath:

    def test_borderline_score_creates_watching_entry(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=40, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        assert wl.watched == 1
        assert wl.promoted == 0
        active = list_watching(db)
        assert len(active) == 1
        assert active[0].symbol == "BTCUSDT"
        assert active[0].last_score == 40
        # The signals table is untouched — borderline does not emit.
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0

    def test_repeated_borderline_does_not_duplicate_row(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=40, severity=None),
        )
        run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW + timedelta(hours=1), sender=_RecordingSender(),
        )
        rows = db.execute(
            "SELECT COUNT(*) FROM watchlist WHERE status='watching'"
        ).fetchone()[0]
        assert rows == 1

    def test_disabled_watchlist_writes_no_rows(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path, watchlist_enabled=False)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=40, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        assert report.watchlist_report is None
        assert db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


# ---------- PROMOTE path ----------

class TestPromotePath:

    def test_borderline_then_qualifying_promotes_and_links(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        # Cycle 1 — borderline: score=40, severity=None -> WATCH
        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=40, severity=None),
        )
        run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        watch = get_watching(db, symbol="BTCUSDT")
        assert watch is not None

        # Cycle 2 — qualifying (severity=None forces the watchlist
        # branch, score=72 >= min_signal_score=50 -> PROMOTE).
        # Use a different candle_hour so dedup never fires.
        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(
                score=72, severity=None,
                detected_at="2026-04-23T16:00:00Z",
            ),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW + timedelta(hours=1), sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        assert wl.promoted == 1
        # The watch transitioned to status='promoted'.
        row = db.execute(
            "SELECT status, promoted_signal_id, resolution_reason "
            "FROM watchlist WHERE id = ?",
            (watch.id,),
        ).fetchone()
        assert row["status"] == "promoted"
        assert row["resolution_reason"] == "promoted"
        sig_id = row["promoted_signal_id"]
        assert sig_id is not None

        # The new signal carries the watchlist linkage AND the synthesized severity.
        sig = db.execute(
            "SELECT id, severity, watchlist_id FROM signals WHERE id = ?",
            (sig_id,),
        ).fetchone()
        assert sig["severity"] == "strong"  # 72 >= severity.strong=65
        assert sig["watchlist_id"] == watch.id

    def test_promotion_with_no_active_watch_still_inserts(
        self, db, tmp_path, monkeypatch
    ):
        """A score >= min_signal_score with severity=None and no
        active watch should insert a signal but leave watchlist_id NULL.
        """
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=72, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        # Without an active watch the report counts the insert under
        # signal_insert_reasons but leaves wl.promoted at 0.
        assert wl.promoted == 0
        sig = db.execute(
            "SELECT severity, watchlist_id FROM signals"
        ).fetchone()
        assert sig is not None
        assert sig["severity"] == "strong"
        assert sig["watchlist_id"] is None


# ---------- EXPIRE / IGNORE paths ----------

class TestExpireAndIgnorePaths:

    def test_below_floor_expires_active_watch(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)
        # Pre-seed an active watch.
        upsert_watching(
            db, symbol="BTCUSDT", score=40,
            now=NOW - timedelta(hours=2), max_watch_hours=48,
        )

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=20, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        assert wl.expired_below_floor == 1
        assert get_watching(db, symbol="BTCUSDT") is None
        row = db.execute(
            "SELECT status, resolution_reason FROM watchlist "
            "WHERE symbol='BTCUSDT'"
        ).fetchone()
        assert row["status"] == "expired"
        assert row["resolution_reason"] == "expired_below_floor"

    def test_below_floor_no_active_watch_ignored(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=20, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        assert wl.ignored == 1
        assert wl.expired_below_floor == 0
        assert db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


# ---------- expire_stale once-per-cycle ----------

class TestExpireStaleAtScanStart:

    def test_stale_entries_are_expired_at_scan_start(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path, max_watch_hours=48)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        # Pre-seed an active watch that is already past its expiry.
        upsert_watching(
            db, symbol="ETHUSDT", score=40,
            now=NOW - timedelta(hours=72), max_watch_hours=48,
        )
        # Sanity: it is still in 'watching' status before the scan.
        assert get_watching(db, symbol="ETHUSDT") is not None

        # The scan only iterates BTCUSDT (only tracked symbol). Stale
        # expiration must run regardless and clear the ETHUSDT row.
        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=40, severity=None),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        wl = report.watchlist_report
        assert wl is not None
        assert wl.expired_stale == 1
        eth_status = db.execute(
            "SELECT status FROM watchlist WHERE symbol='ETHUSDT'"
        ).fetchone()["status"]
        assert eth_status == "expired"


# ---------- regular emit path is unchanged ----------

class TestRegularEmitUnchanged:

    def test_regular_signal_does_not_touch_watchlist(
        self, db, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        seed_default_symbols(db, list(settings.symbols.tracked))
        _seed_one_1h_candle(db)

        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(score=72, severity="strong"),
        )
        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )
        # Regular insert path bumped the inserted counter.
        assert report.inserted_signals == 1
        # Watchlist counters all zero — the non-None severity branch ran first.
        wl = report.watchlist_report
        assert wl is not None
        assert wl.watched == 0
        assert wl.promoted == 0
        assert wl.ignored == 0
        # No watchlist row created.
        assert db.execute(
            "SELECT COUNT(*) FROM watchlist"
        ).fetchone()[0] == 0
        # The emitted signal has watchlist_id NULL.
        sig = db.execute("SELECT watchlist_id FROM signals").fetchone()
        assert sig["watchlist_id"] is None
