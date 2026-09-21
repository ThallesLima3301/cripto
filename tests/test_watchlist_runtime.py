"""Block 23 — watchlist integration into the scan loop.

The scoring engine itself is exhaustively tested elsewhere
(``test_signal_engine.py``). Most state-machine tests monkey-patch
``crypto_monitor.scheduler.entrypoints.score_signal`` to return a
fabricated ``SignalCandidate`` with a chosen ``score`` and
``severity`` so we can drive every state-machine branch
deterministically.

Regime integration regressions at the end use the real scoring engine
and SQLite candles, so an inconsistent fabricated score/severity pair
cannot hide an emission-threshold mismatch.

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
from dataclasses import dataclass, replace
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
from crypto_monitor.regime import RegimeSnapshot
from crypto_monitor.scheduler import run_scan
from crypto_monitor.scheduler.entrypoints import ScanReport, WatchlistReport
from crypto_monitor.signals import score_signal
from crypto_monitor.signals.persistence import load_candles
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

        # Cycle 2: the engine already assigns the qualifying severity.
        # Use a different candle_hour so dedup never fires.
        _patch_score(
            monkeypatch,
            lambda *a, **kw: _candidate(
                score=72, severity="strong",
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

        # The new signal carries the watchlist linkage and engine severity.
        sig = db.execute(
            "SELECT id, severity, watchlist_id FROM signals WHERE id = ?",
            (sig_id,),
        ).fetchone()
        assert sig["severity"] == "strong"  # 72 >= severity.strong=65
        assert sig["watchlist_id"] == watch.id

    def test_promotion_with_no_active_watch_still_inserts(
        self, db, tmp_path, monkeypatch
    ):
        """A score >= min_signal_score with a qualifying severity and no
        active watch should insert a signal but leave watchlist_id NULL.
        """
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
        # With no active watch, emitting a signal needs no watchlist row.
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


# ---------- real engine + regime + watchlist integration ----------

def _real_scoring_settings(
    tmp_path: Path, *, score: int, watchlist_enabled: bool = True,
) -> Settings:
    """Use a single real drop factor to reach exact boundary scores.

    The seeded closed candle falls 10%. With all other factor weights
    zero, its configured drop points are also the actual engine total.
    The normal/strong/very_strong thresholds remain the production
    defaults (50/65/80), including the normal floor affected by risk_on.
    """
    settings = _settings(tmp_path, watchlist_enabled=watchlist_enabled)
    scoring = replace(
        settings.scoring,
        weights=ScoringWeights(100, 0, 0, 0, 0, 0, 0),
        thresholds=replace(settings.scoring.thresholds, drop_1h_points=(score,)),
    )
    return replace(settings, scoring=scoring, regime=replace(settings.regime, enabled=True))


def _seed_real_drop(db: sqlite3.Connection) -> None:
    seed_default_symbols(db, ["BTCUSDT"])
    _seed_one_1h_candle(db)
    db.execute(
        "UPDATE candles SET open=100, high=100, low=90, close=90 "
        "WHERE symbol='BTCUSDT' AND interval='1h'"
    )
    db.commit()


def _snapshot(label: str) -> RegimeSnapshot:
    return RegimeSnapshot(
        label=label, btc_ema_short=100.0, btc_ema_long=100.0,
        btc_atr_14d=1.0, atr_percentile=50.0,
        determined_at="2026-04-23T15:00:00Z",
    )


def _real_candidate(db, settings, label: str) -> SignalCandidate:
    adjustment = {
        "risk_off": settings.regime.threshold_adjust_risk_off,
        "risk_on": settings.regime.threshold_adjust_risk_on,
        "neutral": 0,
    }[label]
    candidate = score_signal(
        "BTCUSDT", load_candles(db, "BTCUSDT", "1h"), [], [],
        settings.scoring, detected_at="2026-04-23T15:00:00Z",
        regime_at_signal=label, min_score_adjust=adjustment,
    )
    assert candidate is not None
    return candidate


def _real_score_cycle(db, settings, label: str) -> ScanReport:
    report = ScanReport(
        watchlist_report=WatchlistReport() if settings.watchlist.enabled else None,
    )
    ENT_MOD._score_and_persist(
        db, symbols=["BTCUSDT"], settings=settings, report=report,
        now=NOW, regime=_snapshot(label),
    )
    return report


class TestRealEngineRegimeIntegration:

    @pytest.mark.parametrize("watchlist_enabled", [True, False])
    @pytest.mark.parametrize("has_watch", [True, False])
    def test_risk_off_never_emits_below_adjusted_floor(
        self, db, tmp_path, watchlist_enabled, has_watch,
    ):
        settings = _real_scoring_settings(
            tmp_path, score=52, watchlist_enabled=watchlist_enabled,
        )
        _seed_real_drop(db)
        watch = None
        if has_watch:
            watch = upsert_watching(
                db, symbol="BTCUSDT", score=40,
                now=NOW - timedelta(hours=1), max_watch_hours=48,
            )

        candidate = _real_candidate(db, settings, "risk_off")
        assert candidate.score == 52
        assert candidate.severity is None  # effective floor is 55
        report = _real_score_cycle(db, settings, "risk_off")

        assert report.errors == []
        assert report.inserted_signals == 0
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        if watchlist_enabled:
            assert report.watchlist_report.watched == 1
            assert report.watchlist_report.promoted == 0
            active = get_watching(db, symbol="BTCUSDT")
            assert active is not None
            assert active.last_score == 52
            if watch is not None:
                assert active.id == watch.id
        else:
            assert report.watchlist_report is None
            assert get_watching(db, symbol="BTCUSDT") == watch

    @pytest.mark.parametrize(
        "label,score,severity",
        [("risk_off", 55, "normal"), ("risk_on", 47, "normal"),
         ("neutral", 72, "strong")],
    )
    @pytest.mark.parametrize("has_watch", [True, False])
    def test_qualifying_engine_candidate_emits_and_resolves_active_watch(
        self, db, tmp_path, label, score, severity, has_watch,
    ):
        settings = _real_scoring_settings(tmp_path, score=score)
        _seed_real_drop(db)
        watch = None
        if has_watch:
            watch = upsert_watching(
                db, symbol="BTCUSDT", score=40,
                now=NOW - timedelta(hours=1), max_watch_hours=48,
            )

        candidate = _real_candidate(db, settings, label)
        assert candidate.score == score
        assert candidate.severity == severity
        report = _real_score_cycle(db, settings, label)

        assert report.errors == []
        assert report.inserted_signals == 1
        assert report.watchlist_report.promoted == int(has_watch)
        signal = db.execute("SELECT * FROM signals").fetchone()
        assert signal["score"] == score
        assert signal["severity"] == severity
        assert signal["regime_at_signal"] == label
        assert signal["watchlist_id"] == (watch.id if watch else None)
        assert get_watching(db, symbol="BTCUSDT") is None
        if watch is not None:
            resolved = db.execute(
                "SELECT * FROM watchlist WHERE id = ?", (watch.id,),
            ).fetchone()
            assert resolved["status"] == "promoted"
            assert resolved["promoted_signal_id"] == signal["id"]
            assert resolved["resolution_reason"] == "promoted"
        else:
            assert db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0

        # Re-evaluating the same closed candle does not duplicate a signal
        # or count a resolved watch as another promotion.
        repeated = _real_score_cycle(db, settings, label)
        assert repeated.errors == []
        assert repeated.inserted_signals == 0
        assert repeated.signal_insert_reasons == {"duplicate": 1}
        assert repeated.watchlist_report.promoted == 0
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == int(has_watch)

    def test_duplicate_does_not_resolve_a_new_active_watch(self, db, tmp_path):
        settings = _real_scoring_settings(tmp_path, score=72)
        _seed_real_drop(db)
        first = _real_score_cycle(db, settings, "neutral")
        assert first.inserted_signals == 1
        watch = upsert_watching(
            db, symbol="BTCUSDT", score=40,
            now=NOW - timedelta(minutes=5), max_watch_hours=48,
        )

        report = _real_score_cycle(db, settings, "neutral")

        assert report.errors == []
        assert report.signal_insert_reasons == {"duplicate": 1}
        assert report.watchlist_report.promoted == 0
        assert get_watching(db, symbol="BTCUSDT") == watch
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        assert db.execute("SELECT watchlist_id FROM signals").fetchone()[0] is None

    @pytest.mark.parametrize("score,expected_status", [(32, "promoted"), (29, "expired")])
    def test_risk_on_floor_below_watch_floor_still_resolves_active_watch(
        self, db, tmp_path, score, expected_status,
    ):
        settings = _real_scoring_settings(tmp_path, score=score)
        settings = replace(
            settings,
            regime=replace(settings.regime, threshold_adjust_risk_on=-20),
        )
        _seed_real_drop(db)
        watch = upsert_watching(
            db, symbol="BTCUSDT", score=40,
            now=NOW - timedelta(hours=1), max_watch_hours=48,
        )
        candidate = _real_candidate(db, settings, "risk_on")
        assert candidate.score == score
        assert candidate.severity == ("normal" if score >= 30 else None)

        report = _real_score_cycle(db, settings, "risk_on")

        assert report.errors == []
        assert get_watching(db, symbol="BTCUSDT") is None
        resolved = db.execute(
            "SELECT * FROM watchlist WHERE id = ?", (watch.id,),
        ).fetchone()
        assert resolved["status"] == expected_status
        if expected_status == "promoted":
            assert report.inserted_signals == 1
            assert report.watchlist_report.promoted == 1
            signal = db.execute("SELECT * FROM signals").fetchone()
            assert signal["watchlist_id"] == watch.id
            assert resolved["promoted_signal_id"] == signal["id"]
        else:
            assert report.inserted_signals == 0
            assert report.watchlist_report.expired_below_floor == 1
            assert resolved["resolution_reason"] == "expired_below_floor"
            assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0

    def test_failed_promotion_rolls_back_both_signal_and_watch_update(
        self, db, tmp_path, monkeypatch,
    ):
        settings = _real_scoring_settings(tmp_path, score=72)
        _seed_real_drop(db)
        watch = upsert_watching(
            db, symbol="BTCUSDT", score=40,
            now=NOW - timedelta(hours=1), max_watch_hours=48,
        )

        def fail_after_watch_update(conn, *, symbol, signal_id, **_):
            conn.execute(
                "UPDATE watchlist SET status='promoted', promoted_signal_id=? "
                "WHERE symbol=? AND status='watching'",
                (signal_id, symbol),
            )
            raise sqlite3.OperationalError("simulated promotion failure")

        monkeypatch.setattr(ENT_MOD, "promote", fail_after_watch_update)
        report = _real_score_cycle(db, settings, "neutral")

        assert report.errors == ["score BTCUSDT: simulated promotion failure"]
        assert report.inserted_signals == 0
        assert report.watchlist_report.promoted == 0
        assert db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert get_watching(db, symbol="BTCUSDT") == watch

    def test_disabled_watchlist_leaves_even_stale_active_watch_untouched(
        self, db, tmp_path,
    ):
        settings = _real_scoring_settings(tmp_path, score=72, watchlist_enabled=False)
        settings = replace(settings, regime=replace(settings.regime, enabled=False))
        _seed_real_drop(db)
        watch = upsert_watching(
            db, symbol="BTCUSDT", score=40,
            now=NOW - timedelta(hours=72), max_watch_hours=48,
        )

        report = run_scan(
            settings=settings, conn=db, client=_NoOpClient(),
            now=NOW, sender=_RecordingSender(),
        )

        assert report.errors == []
        assert report.inserted_signals == 1
        assert report.watchlist_report is None
        assert get_watching(db, symbol="BTCUSDT") == watch
        assert db.execute("SELECT watchlist_id FROM signals").fetchone()[0] is None
