"""Tests for the schema migration framework.

Covers the migration runner, helpers, idempotency, rollback, and
version bookkeeping.  Uses temporary migrations registered via
monkeypatch to avoid coupling to any real v2+ migration.
"""

from __future__ import annotations

import sqlite3

import pytest

from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import (
    BASELINE_VERSION,
    MigrationError,
    MigrationReport,
    _MIGRATIONS,
    _read_version,
    _write_version,
    column_exists,
    run_migrations,
    table_exists,
)
from crypto_monitor.database.schema import SCHEMA_VERSION, init_db


# ---------- fixtures ----------

@pytest.fixture
def fresh_db():
    """In-memory DB with baseline schema via init_db()."""
    conn = get_connection(":memory:")
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def bare_db():
    """In-memory DB with NO tables — simulates a completely blank state."""
    conn = get_connection(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_migrations(monkeypatch):
    """Ensure test migrations don't leak between tests.

    Saves and restores the global _MIGRATIONS dict around each test.
    """
    original = dict(_MIGRATIONS)
    yield
    _MIGRATIONS.clear()
    _MIGRATIONS.update(original)


# ---------- helpers ----------

def _register(target: int, fn):
    """Directly register a migration (bypasses decorator import issues)."""
    _MIGRATIONS[target] = fn


# ---------- BASELINE_VERSION / SCHEMA_VERSION ----------

def test_baseline_version_is_one():
    assert BASELINE_VERSION == 1


def test_schema_version_equals_baseline():
    """schema.py re-exports BASELINE_VERSION as SCHEMA_VERSION."""
    assert SCHEMA_VERSION == BASELINE_VERSION


# ---------- helper: column_exists ----------

def test_column_exists_true(fresh_db):
    assert column_exists(fresh_db, "signals", "symbol") is True


def test_column_exists_false(fresh_db):
    assert column_exists(fresh_db, "signals", "nonexistent_col") is False


def test_column_exists_nonexistent_table(fresh_db):
    """PRAGMA table_info on a missing table returns no rows → False."""
    assert column_exists(fresh_db, "no_such_table", "col") is False


# ---------- helper: table_exists ----------

def test_table_exists_true(fresh_db):
    assert table_exists(fresh_db, "signals") is True


def test_table_exists_false(fresh_db):
    assert table_exists(fresh_db, "no_such_table") is False


# ---------- _read_version / _write_version ----------

def test_read_version_fresh_db(fresh_db):
    """init_db stamps BASELINE_VERSION."""
    assert _read_version(fresh_db) == BASELINE_VERSION


def test_read_version_no_schema_meta(bare_db):
    """No schema_meta table → version 0."""
    assert _read_version(bare_db) == 0


def test_write_version_roundtrip(fresh_db):
    _write_version(fresh_db, 5)
    fresh_db.commit()
    assert _read_version(fresh_db) == 5


# ---------- run_migrations: no migrations registered ----------

def test_no_migrations_returns_baseline(fresh_db):
    """With no migrations registered, run_migrations is a no-op."""
    _MIGRATIONS.clear()
    report = run_migrations(fresh_db)
    assert report.from_version == BASELINE_VERSION
    assert report.to_version == BASELINE_VERSION
    assert report.steps_applied == ()


# ---------- run_migrations: sequential application ----------

def test_single_migration_applies(fresh_db):
    """Register one migration (v1 → v2), verify it runs."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)

    report = run_migrations(fresh_db)
    assert report == MigrationReport(
        from_version=1, to_version=2, steps_applied=(2,)
    )
    assert _read_version(fresh_db) == 2
    assert table_exists(fresh_db, "test_v2")


def test_multiple_migrations_apply_in_order(fresh_db):
    """Register v2 and v3, verify both run sequentially."""
    call_order: list[int] = []

    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )
        call_order.append(2)

    def migrate_v3(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v3 (id INTEGER PRIMARY KEY)"
        )
        call_order.append(3)

    _register(2, migrate_v2)
    _register(3, migrate_v3)

    report = run_migrations(fresh_db)
    assert report.from_version == 1
    assert report.to_version == 3
    assert report.steps_applied == (2, 3)
    assert call_order == [2, 3]
    assert table_exists(fresh_db, "test_v2")
    assert table_exists(fresh_db, "test_v3")
    assert _read_version(fresh_db) == 3


def test_gap_in_migration_numbers(fresh_db):
    """Register v2 and v4 (skip v3) — only registered ones run."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    def migrate_v4(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v4 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)
    _register(4, migrate_v4)

    report = run_migrations(fresh_db)
    assert report.steps_applied == (2, 4)
    assert report.to_version == 4


# ---------- run_migrations: already up to date ----------

def test_already_at_latest_version(fresh_db):
    """If the DB is already at the latest version, nothing runs."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)

    # First run applies it.
    run_migrations(fresh_db)
    assert _read_version(fresh_db) == 2

    # Second run is a no-op.
    report = run_migrations(fresh_db)
    assert report.from_version == 2
    assert report.to_version == 2
    assert report.steps_applied == ()


# ---------- run_migrations: idempotency ----------

def test_migration_idempotent(fresh_db):
    """Running the same migration twice (by resetting version) is safe
    because the migration uses IF NOT EXISTS guards.
    """
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)

    run_migrations(fresh_db)
    assert _read_version(fresh_db) == 2

    # Simulate a crash that rolled back the version bump but not the DDL.
    _write_version(fresh_db, 1)
    fresh_db.commit()

    # Re-running the migration should succeed (CREATE IF NOT EXISTS).
    report = run_migrations(fresh_db)
    assert report.steps_applied == (2,)
    assert _read_version(fresh_db) == 2
    assert table_exists(fresh_db, "test_v2")


def test_add_column_idempotent(fresh_db):
    """A migration that adds a column is idempotent via column_exists guard."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        if not column_exists(conn, "signals", "test_col"):
            conn.execute("ALTER TABLE signals ADD COLUMN test_col TEXT")

    _register(2, migrate_v2)

    run_migrations(fresh_db)
    assert column_exists(fresh_db, "signals", "test_col")

    # Reset version and re-run — should not raise.
    _write_version(fresh_db, 1)
    fresh_db.commit()
    report = run_migrations(fresh_db)
    assert report.steps_applied == (2,)
    assert column_exists(fresh_db, "signals", "test_col")


# ---------- run_migrations: rollback on error ----------

def test_migration_failure_rolls_back(fresh_db):
    """A failing migration rolls back its changes and raises MigrationError."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    def migrate_v3(conn: sqlite3.Connection) -> None:
        # This will fail — deliberate bad SQL.
        conn.execute("THIS IS NOT VALID SQL")

    _register(2, migrate_v2)
    _register(3, migrate_v3)

    with pytest.raises(MigrationError) as exc_info:
        run_migrations(fresh_db)

    assert exc_info.value.version == 3
    # v2 should have been committed before v3 failed.
    assert _read_version(fresh_db) == 2
    assert table_exists(fresh_db, "test_v2")


def test_migration_error_preserves_version(fresh_db):
    """If the only registered migration fails, version stays at baseline."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        raise RuntimeError("simulated failure")

    _register(2, migrate_v2)

    with pytest.raises(MigrationError) as exc_info:
        run_migrations(fresh_db)

    assert exc_info.value.version == 2
    assert isinstance(exc_info.value.original, RuntimeError)
    assert _read_version(fresh_db) == BASELINE_VERSION


# ---------- run_migrations: pre-versioned database ----------

def test_pre_versioned_db_gets_baseline(fresh_db):
    """A DB with tables but a corrupted/missing version row is treated
    as BASELINE_VERSION, and migrations start from 2.
    """
    # Remove the version row to simulate a pre-versioned state.
    fresh_db.execute(
        "DELETE FROM schema_meta WHERE key = 'schema_version'"
    )
    fresh_db.commit()

    call_order: list[int] = []

    def migrate_v2(conn: sqlite3.Connection) -> None:
        call_order.append(2)

    _register(2, migrate_v2)

    report = run_migrations(fresh_db)
    assert report.from_version == BASELINE_VERSION
    assert report.steps_applied == (2,)
    assert call_order == [2]


# ---------- MigrationReport ----------

def test_migration_report_frozen():
    report = MigrationReport(from_version=1, to_version=3, steps_applied=(2, 3))
    assert report.from_version == 1
    assert report.to_version == 3
    assert report.steps_applied == (2, 3)


def test_migration_report_defaults():
    report = MigrationReport(from_version=1, to_version=1)
    assert report.steps_applied == ()


# ---------- integration: init_db + run_migrations ----------

def test_init_db_then_run_migrations_fresh(bare_db):
    """Full lifecycle: init_db creates baseline, run_migrations applies deltas."""
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)

    # Step 1: baseline
    init_db(bare_db)
    assert _read_version(bare_db) == BASELINE_VERSION
    assert table_exists(bare_db, "signals")
    assert not table_exists(bare_db, "test_v2")

    # Step 2: migrations
    report = run_migrations(bare_db)
    assert report.from_version == 1
    assert report.to_version == 2
    assert table_exists(bare_db, "test_v2")


def test_init_db_does_not_run_migrations(bare_db):
    """Verify init_db does NOT call run_migrations — the v2 table must
    not exist after init_db alone.
    """
    def migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS test_v2 (id INTEGER PRIMARY KEY)"
        )

    _register(2, migrate_v2)

    init_db(bare_db)
    assert not table_exists(bare_db, "test_v2")
    assert _read_version(bare_db) == BASELINE_VERSION
