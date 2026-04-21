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
