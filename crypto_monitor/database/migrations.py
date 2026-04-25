"""Schema migration runner.

Applies incremental schema changes to an existing database that already
has the v1 baseline tables (created by `init_db()`).

Design contract:
  * `run_migrations()` NEVER calls `init_db()`.
  * `init_db()` NEVER calls `run_migrations()`.
  * Callers are responsible for calling both in order:
        init_db(conn)          # ensures baseline tables exist
        run_migrations(conn)   # applies any pending deltas

Each migration function is registered in `_MIGRATIONS` keyed by its
target version number.  Migrations run sequentially from
`current_version + 1` up to the highest registered target.  Each one
executes inside a SAVEPOINT so a failure rolls back only that step
and leaves prior migrations committed.

Idempotency: every migration uses guards (`CREATE TABLE IF NOT EXISTS`,
column-existence checks before `ALTER TABLE ADD COLUMN`) so running the
same migration twice is a safe no-op.  This protects against the case
where a migration partially succeeded but the version bump did not
persist (e.g. crash between DDL and metadata update).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


logger = logging.getLogger(__name__)


# The baseline version that `init_db()` stamps on a fresh database.
# This constant is the single source of truth — schema.py imports it.
BASELINE_VERSION = 1


# ---------- types ----------

@dataclass(frozen=True)
class MigrationReport:
    """Result of a `run_migrations()` call."""
    from_version: int
    to_version: int
    steps_applied: tuple[int, ...] = ()


class MigrationError(Exception):
    """Raised when a migration function fails."""

    def __init__(self, version: int, original: Exception) -> None:
        self.version = version
        self.original = original
        super().__init__(
            f"Migration to version {version} failed: {original}"
        )


# ---------- migration registry ----------

# Maps target_version → migration callable.
# Each callable receives an open connection.  The caller manages
# transactions (SAVEPOINT) around the call.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def register_migration(
    target_version: int,
) -> Callable[[Callable[[sqlite3.Connection], None]], Callable[[sqlite3.Connection], None]]:
    """Decorator to register a migration function for a target version.

    Usage::

        @register_migration(2)
        def _migrate_002_regime(conn: sqlite3.Connection) -> None:
            ...
    """
    def decorator(fn: Callable[[sqlite3.Connection], None]) -> Callable[[sqlite3.Connection], None]:
        if target_version in _MIGRATIONS:
            raise ValueError(
                f"Duplicate migration registered for version {target_version}"
            )
        _MIGRATIONS[target_version] = fn
        return fn
    return decorator


# ---------- helpers available to migration functions ----------

def column_exists(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> bool:
    """Check whether *column* exists on *table* via PRAGMA table_info."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # table_info returns rows where index 1 is the column name.
    return any(row[1] == column for row in rows)


def table_exists(
    conn: sqlite3.Connection,
    table: str,
) -> bool:
    """Check whether *table* exists in the database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


# ---------- version bookkeeping ----------

def _read_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version from schema_meta.

    Returns BASELINE_VERSION if the table exists but no version row is
    found (defensive — init_db always writes one).  Returns 0 if the
    schema_meta table itself doesn't exist (should not happen after
    init_db, but handled for safety).
    """
    if not table_exists(conn, "schema_meta"):
        return 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return BASELINE_VERSION
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return BASELINE_VERSION


def _write_version(conn: sqlite3.Connection, version: int) -> None:
    """Update the schema_version row in schema_meta."""
    ts = to_utc_iso(now_utc())
    conn.execute(
        """
        UPDATE schema_meta
        SET value = ?, updated_at = ?
        WHERE key = 'schema_version'
        """,
        (str(version), ts),
    )


# ---------- runner ----------

def run_migrations(conn: sqlite3.Connection) -> MigrationReport:
    """Apply all pending migrations in order.

    Precondition: `init_db(conn)` has already been called so the
    baseline tables and schema_meta row exist.

    Each migration runs inside a SAVEPOINT.  On success the savepoint
    is released and the version is bumped.  On failure the savepoint
    is rolled back, a MigrationError is raised, and all previously
    applied migrations in this call remain committed.
    """
    if not _MIGRATIONS:
        current = _read_version(conn)
        return MigrationReport(
            from_version=current,
            to_version=current,
        )

    current = _read_version(conn)
    start = current
    applied: list[int] = []

    max_target = max(_MIGRATIONS.keys())
    if current >= max_target:
        return MigrationReport(
            from_version=current,
            to_version=current,
        )

    for target in range(current + 1, max_target + 1):
        if target not in _MIGRATIONS:
            continue
        migration_fn = _MIGRATIONS[target]
        savepoint = f"migration_v{target}"

        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            migration_fn(conn)
            _write_version(conn, target)
            conn.execute(f"RELEASE {savepoint}")
            current = target
            applied.append(target)
            logger.info("migration to version %d applied", target)
        except Exception as exc:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            logger.error(
                "migration to version %d failed: %s", target, exc
            )
            raise MigrationError(target, exc) from exc

    if applied:
        conn.commit()

    return MigrationReport(
        from_version=start,
        to_version=current,
        steps_applied=tuple(applied),
    )


# =====================================================================
# Registered migrations
# =====================================================================

@register_migration(2)
def _migrate_002_regime(conn: sqlite3.Connection) -> None:
    """Add regime_snapshots table and signals.regime_at_signal column."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regime_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            label           TEXT NOT NULL,
            btc_ema_short   REAL NOT NULL,
            btc_ema_long    REAL NOT NULL,
            btc_atr_14d     REAL NOT NULL,
            atr_percentile  REAL NOT NULL,
            determined_at   TEXT NOT NULL
        )
    """)
    if not column_exists(conn, "signals", "regime_at_signal"):
        conn.execute(
            "ALTER TABLE signals ADD COLUMN regime_at_signal TEXT"
        )


@register_migration(3)
def _migrate_003_sell(conn: sqlite3.Connection) -> None:
    """Sell-engine data model (Block 19, schema only).

    Adds the two persistence tables the sell engine will use plus three
    nullable columns on ``buys`` so a position can be marked sold
    without breaking any existing reader. No business logic is wired
    yet — this migration is purely about shape.

      * ``sell_tracking``  — one high-water-mark row per (symbol, buy_id)
        used by the trailing-stop rule. PRIMARY KEY (symbol, buy_id)
        keeps it idempotent and lets ``upsert`` use ON CONFLICT.
      * ``sell_signals``   — append-only log of sell-side decisions.
        ``buy_id`` is a FK so a sell signal is always anchored to the
        position it refers to. ``rule_triggered`` is a free TEXT so
        future rules can be added without a schema change.
      * ``buys.sold_at`` / ``sold_price`` / ``sold_note`` — nullable so
        every existing buy row remains valid (open position).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sell_tracking (
            symbol            TEXT NOT NULL,
            buy_id            INTEGER NOT NULL REFERENCES buys(id),
            high_watermark    REAL NOT NULL,
            updated_at        TEXT NOT NULL,
            PRIMARY KEY (symbol, buy_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sell_signals (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol             TEXT NOT NULL,
            buy_id             INTEGER NOT NULL REFERENCES buys(id),
            detected_at        TEXT NOT NULL,
            price_at_signal    REAL NOT NULL,
            rule_triggered     TEXT NOT NULL,
            severity           TEXT NOT NULL,
            reason             TEXT NOT NULL,
            pnl_pct            REAL,
            regime_at_signal   TEXT,
            alerted            INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sell_signals_symbol_time
        ON sell_signals (symbol, detected_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sell_signals_buy
        ON sell_signals (buy_id, detected_at DESC)
    """)
    if not column_exists(conn, "buys", "sold_at"):
        conn.execute("ALTER TABLE buys ADD COLUMN sold_at TEXT")
    if not column_exists(conn, "buys", "sold_price"):
        conn.execute("ALTER TABLE buys ADD COLUMN sold_price REAL")
    if not column_exists(conn, "buys", "sold_note"):
        conn.execute("ALTER TABLE buys ADD COLUMN sold_note TEXT")


@register_migration(4)
def _migrate_004_watchlist(conn: sqlite3.Connection) -> None:
    """Watchlist data model (Block 22, schema only).

    The watchlist tracks "borderline" setups — scores sitting between
    ``watchlist.floor_score`` and ``scoring.thresholds.min_signal_score``
    — so they can be promoted to a real buy signal if the score climbs
    past the emit floor, or be quietly aged out if it drifts. Block 22
    only ships the persistence shape:

      * ``watchlist``                      — append-only log with exactly
        one ``status='watching'`` row per symbol, enforced by a partial
        unique index. Resolved rows (promoted / expired) remain so the
        history is inspectable.
      * ``signals.watchlist_id``           — nullable FK so a signal
        emitted from a promoted watch can be traced back to the
        originating watch row. Every existing row stays valid because
        the column is nullable.

    Idempotent via ``CREATE ... IF NOT EXISTS`` and ``column_exists``
    guards.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                TEXT NOT NULL,
            status                TEXT NOT NULL,
            first_seen_at         TEXT NOT NULL,
            last_seen_at          TEXT NOT NULL,
            last_score            INTEGER NOT NULL,
            expires_at            TEXT NOT NULL,
            promoted_signal_id    INTEGER REFERENCES signals(id),
            resolved_at           TEXT,
            resolution_reason     TEXT
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_active_symbol
        ON watchlist (symbol)
        WHERE status = 'watching'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_watchlist_status_expires
        ON watchlist (status, expires_at)
    """)
    if not column_exists(conn, "signals", "watchlist_id"):
        conn.execute(
            "ALTER TABLE signals ADD COLUMN watchlist_id INTEGER "
            "REFERENCES watchlist(id)"
        )


@register_migration(5)
def _migrate_005_eval_timing(conn: sqlite3.Connection) -> None:
    """MFE/MAE timing extensions for evaluation rows (Block 24).

    Adds nullable timing/peak columns to the evaluation tables:

      * ``signal_evaluations.time_to_mfe_hours``  — hours from
        ``candle_hour`` to the bar that produced the maximum favorable
        excursion (MFE) inside the 7-day window.
      * ``signal_evaluations.time_to_mae_hours``  — same for MAE.
      * ``buy_evaluations.max_gain_pct``          — MFE % over the 7-day
        post-buy window. Mirrors signal_evaluations.max_gain_7d_pct
        but the column is named without the horizon so a future
        config-driven window can reuse it.
      * ``buy_evaluations.max_loss_pct``          — MAE % counterpart.
      * ``buy_evaluations.time_to_mfe_hours``     — hours from buy
        time to the MFE bar.
      * ``buy_evaluations.time_to_mae_hours``     — hours from buy
        time to the MAE bar.

    Every column is nullable; pre-existing rows keep their semantics and
    receive NULL for the new fields. Idempotent via ``column_exists``
    guards on each ``ALTER TABLE``.
    """
    if not column_exists(conn, "signal_evaluations", "time_to_mfe_hours"):
        conn.execute(
            "ALTER TABLE signal_evaluations ADD COLUMN time_to_mfe_hours REAL"
        )
    if not column_exists(conn, "signal_evaluations", "time_to_mae_hours"):
        conn.execute(
            "ALTER TABLE signal_evaluations ADD COLUMN time_to_mae_hours REAL"
        )
    if not column_exists(conn, "buy_evaluations", "max_gain_pct"):
        conn.execute(
            "ALTER TABLE buy_evaluations ADD COLUMN max_gain_pct REAL"
        )
    if not column_exists(conn, "buy_evaluations", "max_loss_pct"):
        conn.execute(
            "ALTER TABLE buy_evaluations ADD COLUMN max_loss_pct REAL"
        )
    if not column_exists(conn, "buy_evaluations", "time_to_mfe_hours"):
        conn.execute(
            "ALTER TABLE buy_evaluations ADD COLUMN time_to_mfe_hours REAL"
        )
    if not column_exists(conn, "buy_evaluations", "time_to_mae_hours"):
        conn.execute(
            "ALTER TABLE buy_evaluations ADD COLUMN time_to_mae_hours REAL"
        )
