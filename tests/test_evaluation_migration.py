"""Upgrade legacy evaluation results without losing the original history."""

from __future__ import annotations

import pytest

from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import (
    MigrationError,
    _MIGRATIONS,
    _read_version,
    run_migrations,
    table_exists,
)
from crypto_monitor.database.schema import init_db


_EVALUATION_TABLES = ("signal_evaluations", "buy_evaluations")


@pytest.fixture
def legacy_db(monkeypatch):
    """Real schema through v5, containing complete and partial outcomes."""
    conn = get_connection(":memory:")
    init_db(conn)
    with monkeypatch.context() as patch:
        patch.delitem(_MIGRATIONS, 6)
        run_migrations(conn)
    assert _read_version(conn) == 5

    conn.execute("""
        INSERT INTO signals (
            id, symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal, score_breakdown
        ) VALUES (
            11, 'BTCUSDT', '2026-01-01T15:00:00Z',
            '2026-01-01T14:00:00Z', 100.0, 70, 'strong', 'test', 0, '{}'
        )
    """)
    conn.execute("""
        INSERT INTO buys (
            id, symbol, bought_at, price, amount_invested,
            quote_currency, quantity, signal_id, created_at
        ) VALUES (
            23, 'BTCUSDT', '2026-01-01T15:30:00Z', 101.0,
            202.0, 'USDT', 2.0, 11, '2026-01-01T16:00:00Z'
        )
    """)
    conn.execute("""
        INSERT INTO signal_evaluations (
            id, signal_id, evaluated_at, price_at_signal,
            price_24h_later, price_7d_later, price_30d_later,
            return_24h_pct, return_7d_pct, return_30d_pct,
            max_gain_7d_pct, max_loss_7d_pct, verdict,
            time_to_mfe_hours, time_to_mae_hours
        ) VALUES (
            41, 11, '2026-02-02T00:00:00Z', 100.0,
            105.0, 110.0, 120.0, 5.0, 10.0, 20.0,
            50.0, -15.0, 'great', 0.0, 96.0
        )
    """)
    conn.execute("""
        INSERT INTO buy_evaluations (
            id, buy_id, evaluated_at, day_open, day_low_hourly,
            day_low_hourly_time, pct_from_day_open_to_low_hourly,
            pct_from_buy_to_low_hourly, buy_vs_day_low_hourly_pct,
            price_7d_later, return_7d_pct, price_30d_later,
            return_30d_pct, verdict, resolution_note, max_gain_pct,
            max_loss_pct, time_to_mfe_hours, time_to_mae_hours
        ) VALUES (
            57, 23, '2026-02-02T01:00:00Z', 100.0, 90.0,
            '2026-01-01T10:00:00Z', -10.0, -10.8910891089,
            12.2222222222, NULL, NULL, 120.0, 18.8118811881,
            'pending', 'hourly-resolution intraday low',
            30.0, -12.0, 22.5, 100.5
        )
    """)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn, table):
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_v6_archives_every_value_and_only_clears_derived_results(legacy_db):
    originals = {table: _rows(legacy_db, table) for table in _EVALUATION_TABLES}
    signals = _rows(legacy_db, "signals")
    buys = _rows(legacy_db, "buys")

    report = run_migrations(legacy_db)

    assert report.from_version == 5
    assert report.to_version == 6
    assert report.steps_applied == (6,)
    for table, rows in originals.items():
        assert _rows(legacy_db, f"{table}_legacy_v1") == rows
        assert _rows(legacy_db, table) == []
    assert _rows(legacy_db, "signals") == signals
    assert _rows(legacy_db, "buys") == buys


def test_repeated_startup_keeps_recalculated_rows_and_archive(legacy_db):
    originals = {table: _rows(legacy_db, table) for table in _EVALUATION_TABLES}
    run_migrations(legacy_db)
    for table in _EVALUATION_TABLES:
        legacy_db.execute(f"INSERT INTO {table} SELECT * FROM {table}_legacy_v1")
        legacy_db.execute(
            f"UPDATE {table} SET return_7d_pct = 2.5, "
            "verdict = 'neutral', evaluated_at = '2026-02-03T00:00:00Z'"
        )
    legacy_db.commit()
    corrected = {table: _rows(legacy_db, table) for table in _EVALUATION_TABLES}

    # Startup calls init_db before run_migrations and currently resets
    # schema_meta to the baseline. The archives must still guard the data.
    init_db(legacy_db)
    run_migrations(legacy_db)
    assert run_migrations(legacy_db).steps_applied == ()

    for table in _EVALUATION_TABLES:
        assert _rows(legacy_db, table) == corrected[table]
        assert _rows(legacy_db, f"{table}_legacy_v1") == originals[table]


@pytest.mark.parametrize("already_archived", _EVALUATION_TABLES)
def test_existing_archive_guards_only_its_own_table(legacy_db, already_archived):
    archive = f"{already_archived}_legacy_v1"
    legacy_db.execute(f"CREATE TABLE {archive} AS SELECT * FROM {already_archived}")
    legacy_db.execute(f"UPDATE {already_archived} SET return_7d_pct = 2.5")
    legacy_db.commit()
    current = _rows(legacy_db, already_archived)
    archived = _rows(legacy_db, archive)
    other = next(table for table in _EVALUATION_TABLES if table != already_archived)
    other_original = _rows(legacy_db, other)

    run_migrations(legacy_db)

    assert _rows(legacy_db, already_archived) == current
    assert _rows(legacy_db, archive) == archived
    assert _rows(legacy_db, other) == []
    assert _rows(legacy_db, f"{other}_legacy_v1") == other_original


def test_failed_archive_upgrade_rolls_back_both_tables(legacy_db):
    originals = {table: _rows(legacy_db, table) for table in _EVALUATION_TABLES}
    legacy_db.execute("""
        CREATE TRIGGER prevent_buy_eval_delete BEFORE DELETE ON buy_evaluations
        BEGIN SELECT RAISE(ABORT, 'simulated archival failure'); END
    """)
    legacy_db.commit()

    with pytest.raises(MigrationError) as error:
        run_migrations(legacy_db)

    assert error.value.version == 6
    assert _read_version(legacy_db) == 5
    for table in _EVALUATION_TABLES:
        assert _rows(legacy_db, table) == originals[table]
        assert not table_exists(legacy_db, f"{table}_legacy_v1")
