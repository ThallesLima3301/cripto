"""Block 22 — watchlist data model + state machine.

Tests cover the three deliverables:

  * migration 004 shape (``watchlist`` table, partial unique index,
    ``signals.watchlist_id`` column, idempotency).
  * ``WatchlistSettings`` defaults + TOML parse.
  * pure state machine in ``watchlist.manager``.
  * SQL helpers in ``watchlist.store`` — upsert, promote, expire_stale,
    expire_below_floor, get/list, one-watching-per-symbol invariant.

Every test uses an in-memory database and never crosses a process
boundary; the subsystem has no scheduler or notification hooks yet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crypto_monitor.config.settings import load_settings
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import (
    column_exists,
    run_migrations,
    table_exists,
)
from crypto_monitor.database.schema import init_db
from crypto_monitor.watchlist import (
    EXPIRE,
    IGNORE,
    PROMOTE,
    WATCH,
    WATCH_ACTIONS,
    decide_watch_action,
    expire_below_floor,
    expire_stale,
    get_watching,
    list_watching,
    promote,
    upsert_watching,
)


UTC = timezone.utc


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


def _insert_signal(conn: sqlite3.Connection, *, symbol: str = "BTCUSDT") -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown, alerted
        ) VALUES (?, ?, ?, ?, ?, ?, 'test', 0, '{}', 0)
        """,
        (symbol, "2026-04-23T15:00:00Z", "2026-04-23T15:00:00Z",
         100.0, 52, "normal"),
    )
    conn.commit()
    return int(cur.lastrowid)


# =====================================================================
# migration 004
# =====================================================================

class TestMigration004Schema:

    def test_watchlist_table_created(self, db):
        assert table_exists(db, "watchlist")

    def test_signals_has_watchlist_id(self, db):
        assert column_exists(db, "signals", "watchlist_id")

    def test_partial_unique_index_on_active_symbol(self, db):
        now = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
        upsert_watching(db, symbol="BTCUSDT", score=40, now=now,
                        max_watch_hours=48)
        # A second 'watching' row for BTCUSDT must be impossible —
        # any raw INSERT should hit the partial unique index.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO watchlist
                    (symbol, status, first_seen_at, last_seen_at,
                     last_score, expires_at)
                VALUES ('BTCUSDT', 'watching', ?, ?, ?, ?)
                """,
                ("2026-04-23T15:30:00Z", "2026-04-23T15:30:00Z", 41,
                 "2026-04-25T15:30:00Z"),
            )

    def test_resolved_rows_do_not_block_new_watch(self, db):
        """A promoted/expired row on the same symbol is fine — the
        partial index only covers status='watching'."""
        now = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)
        upsert_watching(db, symbol="BTCUSDT", score=40, now=now,
                        max_watch_hours=48)
        assert expire_below_floor(db, symbol="BTCUSDT", now=now)
        # Now a brand-new watch should insert without error.
        entry = upsert_watching(
            db, symbol="BTCUSDT", score=41,
            now=now + timedelta(hours=2), max_watch_hours=48,
        )
        assert entry.status == "watching"

    def test_migration_is_idempotent(self, db):
        report = run_migrations(db)
        assert report.steps_applied == ()
        assert table_exists(db, "watchlist")
        assert column_exists(db, "signals", "watchlist_id")


# =====================================================================
# WatchlistSettings loader
# =====================================================================

class TestWatchlistSettingsLoader:

    def test_example_config_parses_with_defaults(self):
        settings = load_settings(Path("."))
        assert settings.watchlist.enabled is False
        assert settings.watchlist.floor_score == 35
        assert settings.watchlist.max_watch_hours == 48


# =====================================================================
# pure state machine
# =====================================================================

class TestDecideWatchAction:

    def test_watch_actions_constants(self):
        assert WATCH_ACTIONS == ("WATCH", "PROMOTE", "EXPIRE", "IGNORE")

    def test_promote_when_score_at_or_above_emit_floor(self):
        for has in (False, True):
            assert decide_watch_action(
                score=50, min_signal_score=50, floor_score=35,
                has_active_watch=has,
            ) == PROMOTE
            assert decide_watch_action(
                score=72, min_signal_score=50, floor_score=35,
                has_active_watch=has,
            ) == PROMOTE

    def test_watch_in_borderline_band(self):
        # score in [floor_score, min_signal_score) -> WATCH
        for has in (False, True):
            assert decide_watch_action(
                score=35, min_signal_score=50, floor_score=35,
                has_active_watch=has,
            ) == WATCH
            assert decide_watch_action(
                score=49, min_signal_score=50, floor_score=35,
                has_active_watch=has,
            ) == WATCH

    def test_below_floor_without_active_watch_ignores(self):
        assert decide_watch_action(
            score=20, min_signal_score=50, floor_score=35,
            has_active_watch=False,
        ) == IGNORE

    def test_below_floor_with_active_watch_expires(self):
        assert decide_watch_action(
            score=20, min_signal_score=50, floor_score=35,
            has_active_watch=True,
        ) == EXPIRE

    def test_rejects_inverted_thresholds(self):
        with pytest.raises(ValueError, match="floor_score"):
            decide_watch_action(
                score=40, min_signal_score=30, floor_score=35,
                has_active_watch=False,
            )

    def test_equal_thresholds_collapse_watch_band(self):
        """floor_score == min_signal_score: borderline band is empty,
        scores either promote or (if active) expire."""
        assert decide_watch_action(
            score=50, min_signal_score=50, floor_score=50,
            has_active_watch=False,
        ) == PROMOTE
        # 49 is below floor AND below emit — IGNORE (no active) or
        # EXPIRE (active).
        assert decide_watch_action(
            score=49, min_signal_score=50, floor_score=50,
            has_active_watch=False,
        ) == IGNORE
        assert decide_watch_action(
            score=49, min_signal_score=50, floor_score=50,
            has_active_watch=True,
        ) == EXPIRE


# =====================================================================
# store helpers
# =====================================================================

NOW = datetime(2026, 4, 23, 15, 0, tzinfo=UTC)


class TestUpsertWatching:

    def test_inserts_new_row_with_first_seen(self, db):
        entry = upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        assert entry.id > 0
        assert entry.symbol == "BTCUSDT"
        assert entry.status == "watching"
        assert entry.first_seen_at == "2026-04-23T15:00:00Z"
        assert entry.last_seen_at == "2026-04-23T15:00:00Z"
        assert entry.last_score == 40
        assert entry.expires_at == "2026-04-25T15:00:00Z"
        assert entry.promoted_signal_id is None
        assert entry.resolved_at is None
        assert entry.resolution_reason is None

    def test_update_refreshes_last_seen_and_extends_expires(self, db):
        first = upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        later = NOW + timedelta(hours=3)
        second = upsert_watching(
            db, symbol="BTCUSDT", score=45, now=later, max_watch_hours=48,
        )
        assert second.id == first.id  # same row, not a new one
        assert second.first_seen_at == first.first_seen_at
        assert second.last_seen_at == "2026-04-23T18:00:00Z"
        assert second.last_score == 45
        # expires_at rolled forward by the new observation.
        assert second.expires_at == "2026-04-25T18:00:00Z"

    def test_rejects_naive_now(self, db):
        with pytest.raises(ValueError, match="timezone-aware"):
            upsert_watching(
                db, symbol="BTCUSDT", score=40,
                now=datetime(2026, 4, 23, 15, 0),
                max_watch_hours=48,
            )

    def test_rejects_non_positive_max_watch_hours(self, db):
        with pytest.raises(ValueError, match="max_watch_hours"):
            upsert_watching(
                db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=0,
            )


class TestPromote:

    def test_no_active_watch_returns_none(self, db):
        # A signal id is required by promote() but we never reach the update.
        signal_id = _insert_signal(db)
        result = promote(db, symbol="BTCUSDT", signal_id=signal_id, now=NOW)
        assert result is None

    def test_transitions_active_watch_and_links_signal(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        signal_id = _insert_signal(db)
        promoted_at = NOW + timedelta(hours=5)
        resolved = promote(
            db, symbol="BTCUSDT", signal_id=signal_id, now=promoted_at,
        )
        assert resolved is not None
        assert resolved.status == "promoted"
        assert resolved.promoted_signal_id == signal_id
        assert resolved.resolved_at == "2026-04-23T20:00:00Z"
        assert resolved.resolution_reason == "promoted"
        # No active watch remains.
        assert get_watching(db, symbol="BTCUSDT") is None

    def test_promotion_frees_symbol_for_new_watch(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        signal_id = _insert_signal(db)
        promote(db, symbol="BTCUSDT", signal_id=signal_id, now=NOW)
        fresh = upsert_watching(
            db, symbol="BTCUSDT", score=38,
            now=NOW + timedelta(days=1), max_watch_hours=48,
        )
        assert fresh.status == "watching"


class TestExpireStale:

    def test_nothing_to_expire_returns_zero(self, db):
        assert expire_stale(db, now=NOW) == 0

    def test_expires_only_rows_past_deadline(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        upsert_watching(
            db, symbol="ETHUSDT", score=42, now=NOW, max_watch_hours=48,
        )
        # Advance past BTC's expires_at but keep ETH alive by bumping it.
        future = NOW + timedelta(hours=49)
        upsert_watching(
            db, symbol="ETHUSDT", score=43,
            now=future, max_watch_hours=48,
        )
        count = expire_stale(db, now=future)
        assert count == 1
        btc = db.execute(
            "SELECT status, resolution_reason FROM watchlist "
            "WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert btc["status"] == "expired"
        assert btc["resolution_reason"] == "expired_stale"
        eth = get_watching(db, symbol="ETHUSDT")
        assert eth is not None and eth.status == "watching"


class TestExpireBelowFloor:

    def test_no_active_watch_returns_false(self, db):
        assert expire_below_floor(db, symbol="BTCUSDT", now=NOW) is False

    def test_transitions_active_watch_with_reason(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        assert expire_below_floor(
            db, symbol="BTCUSDT", now=NOW + timedelta(hours=1)
        ) is True
        row = db.execute(
            "SELECT status, resolution_reason FROM watchlist "
            "WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert row["status"] == "expired"
        assert row["resolution_reason"] == "expired_below_floor"


class TestListAndGet:

    def test_list_watching_returns_only_active(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        upsert_watching(
            db, symbol="ETHUSDT", score=42, now=NOW, max_watch_hours=48,
        )
        signal_id = _insert_signal(db, symbol="ETHUSDT")
        promote(db, symbol="ETHUSDT", signal_id=signal_id, now=NOW)

        rows = list_watching(db)
        assert [r.symbol for r in rows] == ["BTCUSDT"]

    def test_get_watching_symbol_specific(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        assert get_watching(db, symbol="BTCUSDT") is not None
        assert get_watching(db, symbol="ETHUSDT") is None


# =====================================================================
# one-watching-per-symbol invariant via the public helpers
# =====================================================================

class TestOneWatchingPerSymbol:

    def test_upsert_does_not_create_duplicate(self, db):
        upsert_watching(
            db, symbol="BTCUSDT", score=40, now=NOW, max_watch_hours=48,
        )
        upsert_watching(
            db, symbol="BTCUSDT", score=42,
            now=NOW + timedelta(hours=1), max_watch_hours=48,
        )
        count = db.execute(
            "SELECT COUNT(*) FROM watchlist "
            "WHERE symbol = 'BTCUSDT' AND status = 'watching'"
        ).fetchone()[0]
        assert count == 1
