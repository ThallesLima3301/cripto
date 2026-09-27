"""Exercise the real paper CLI, SQL loader and accounting on temporary files."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crypto_monitor.cli.main import main
from crypto_monitor.paper.service import load_inputs
from crypto_monitor.paper.types import SimulationConfig


UTC = timezone.utc
START = datetime(2025, 1, 1, tzinfo=UTC)


def stamp(hours: float) -> str:
    return (START + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def paper_db(tmp_path: Path) -> Path:
    path = tmp_path / "recorded #signals.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE signals (id INTEGER PRIMARY KEY, symbol TEXT, detected_at TEXT,
                              candle_hour TEXT, score REAL);
        CREATE TABLE notifications (id INTEGER PRIMARY KEY, signal_id INTEGER,
                                    delivered INTEGER, sent_at TEXT);
        CREATE TABLE candles (symbol TEXT, interval TEXT, open_time TEXT,
                              open REAL, close REAL);
        PRAGMA user_version = 42;
    """)
    conn.execute("INSERT INTO signals VALUES (1, 'ETHUSDT', ?, ?, 80)", (stamp(1 / 6), stamp(-1)))
    conn.execute("INSERT INTO notifications VALUES (1, 1, 1, ?)", (stamp(1 / 3),))
    for hour in range(6):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            close = 120 if symbol == "ETHUSDT" and hour == 2 else 110 if hour == 5 else 100
            conn.execute("INSERT INTO candles VALUES (?, '1h', ?, 100, ?)", (symbol, stamp(hour), close))
    conn.commit()
    conn.close()
    return path


def run_paper(db: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main([
        "paper", "simulate", "--db", str(db), "--from", stamp(0), "--until", stamp(6),
        "--capital", "1000", "--position-pct", "100", "--max-positions", "1",
        "--holding-hours", "2", *extra,
    ], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_net_results_and_complete_json_without_mutating_database(paper_db: Path, monkeypatch) -> None:
    cli = sys.modules["crypto_monitor.cli.main"]

    def unexpected(*args, **kwargs):
        pytest.fail("Paper analysis must not initialize, migrate, or load settings with --db")

    for name in ("load_settings", "get_connection", "init_db", "run_migrations"):
        monkeypatch.setattr(cli, name, unexpected)
    before = hashlib.sha256(paper_db.read_bytes()).digest()
    code, out, err = run_paper(paper_db, "--fee-bps", "100", "--slippage-bps", "100", "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    quantity = 1000 / 1.01 / 101
    ending = quantity * 120 * 0.99 * 0.99
    assert report["ending_equity"] == pytest.approx(ending)
    assert report["net_profit"] == pytest.approx(ending - 1000)
    assert report["closed_trades"] == 1
    assert report["open_positions"] == 0
    assert report["trades"][0]["entry_at"] == stamp(1)
    assert report["trades"][0]["exit_at"] == stamp(3)
    assert report["trades"][0]["quantity"] == pytest.approx(quantity)
    assert report["benchmark"]["ending_equity"] == pytest.approx(quantity * 110 * 0.99 * 0.99)
    assert report["equity_curve"]
    assert hashlib.sha256(paper_db.read_bytes()).digest() == before
    with sqlite3.connect(paper_db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'schema_meta'").fetchone() is None


def test_read_only_connection_is_enforced(paper_db: Path, monkeypatch) -> None:
    from crypto_monitor.paper import service
    actual_load = service.load_inputs

    def attempt_write(conn, **kwargs):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM signals")
        return actual_load(conn, **kwargs)

    monkeypatch.setattr(service, "load_inputs", attempt_write)
    code, _, err = run_paper(paper_db)
    assert (code, err) == (0, "")


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "new.db"
    code, out, err = run_paper(missing)
    assert code == 1 and not out
    assert "não encontrado" in err
    assert not missing.parent.exists()


def test_human_report_labels_simulation_costs_and_benchmark(paper_db: Path) -> None:
    code, out, err = run_paper(paper_db)
    assert (code, err) == (0, "")
    for text in ("CARTEIRA SIMULADA", "sem ordens reais", "Lucro/prejuízo líquido", "Maior queda", "BTCUSDT", "10 bps", "5 bps"):
        assert text in out


@pytest.mark.parametrize("extra, expected", [
    (("--from", "2025-01-01T00:00:00"), "fuso UTC"),
    (("--from", "2025-01-01T00:30:00Z"), "hora UTC completa"),
    (("--from", "invalid"), "ISO-8601"),
    (("--until", stamp(0)), "anterior"),
    (("--until", "2999-01-01T00:00:00Z"), "futuro"),
    (("--validation-from", stamp(0)), "entre"),
    (("--validation-from", stamp(6)), "entre"),
    (("--capital", "-1"), "capital"),
])
def test_invalid_periods_and_parameters_fail_without_output(paper_db: Path, extra, expected) -> None:
    code, out, err = run_paper(paper_db, *extra)
    assert code == 1 and not out
    assert expected.lower() in err.lower()


def test_missing_benchmark_hour_fails_instead_of_skipping_gap(paper_db: Path) -> None:
    with sqlite3.connect(paper_db) as conn:
        conn.execute("DELETE FROM candles WHERE symbol = 'BTCUSDT' AND open_time = ?", (stamp(2),))
    code, out, err = run_paper(paper_db)
    assert code == 1 and not out
    assert "BTCUSDT" in err


def test_undelivered_alerts_require_explicit_research_mode(paper_db: Path) -> None:
    with sqlite3.connect(paper_db) as conn:
        conn.execute("UPDATE notifications SET delivered = 0")
    code, out, err = run_paper(paper_db, "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["closed_trades"] == 0
    assert sum(report["skipped"].values()) == 1
    code, out, err = run_paper(paper_db, "--signal-source", "detected", "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["closed_trades"] == 1
    assert report["config"]["signal_source"] == "detected"


def test_late_delivery_and_first_successful_delivery(paper_db: Path) -> None:
    with sqlite3.connect(paper_db) as conn:
        conn.execute("UPDATE notifications SET sent_at = ?", (stamp(2.5),))
        conn.execute("INSERT INTO notifications VALUES (2, 1, 0, ?)", (stamp(0.5),))
        conn.execute("INSERT INTO notifications VALUES (3, 1, 1, ?)", (stamp(4.5),))
    code, out, err = run_paper(paper_db, "--from", stamp(2), "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["trades"][0]["entry_at"] == stamp(3)


def test_validation_period_resets_capital_and_keeps_settings(paper_db: Path) -> None:
    with sqlite3.connect(paper_db) as conn:
        conn.execute("INSERT INTO signals VALUES (2, 'ETHUSDT', ?, ?, 80)", (stamp(3.1), stamp(2)))
        conn.execute("INSERT INTO notifications VALUES (2, 2, 1, ?)", (stamp(3.2),))
    code, out, err = run_paper(paper_db, "--validation-from", stamp(3), "--json")
    assert (code, err) == (0, "")
    result = json.loads(out)
    first, later = result["development"], result["validation"]
    assert first["config"] == later["config"]
    assert first["config"]["initial_capital"] == 1000
    assert first["end"] == later["start"] == stamp(3)
    assert [trade["signal_id"] for trade in first["trades"]] == [1]
    assert [trade["signal_id"] for trade in later["trades"]] == [2]
    assert later["trades"][0]["allocated"] == pytest.approx(1000)
    assert "não prova lucro futuro" in result["validation_note"]


def test_unexpired_positions_remain_open_in_json(paper_db: Path) -> None:
    code, out, err = run_paper(paper_db, "--holding-hours", "168", "--json")
    assert (code, err) == (0, "")
    report = json.loads(out)
    assert report["closed_trades"] == 0
    assert report["open_positions"] == 1
    assert report["trades"][0]["exit_at"] is None
    assert report["trades"][0]["net_pnl"] is None
    assert report["win_rate_pct"] is None


def test_loader_bounds_prices_to_period_and_ignores_other_intervals(paper_db: Path) -> None:
    conn = sqlite3.connect(paper_db)
    conn.row_factory = sqlite3.Row
    try:
        conn.executemany("INSERT INTO candles VALUES ('BTCUSDT', ?, ?, 1, 999)", [
            ("1h", stamp(-1)), ("1h", stamp(6)), ("4h", stamp(0)),
        ])
        signals, bars = load_inputs(conn, start=START, end=START + timedelta(hours=6), config=SimulationConfig())
        assert len(signals) == 1
        assert len(bars) == 12
        assert all(START <= bar.open_time < START + timedelta(hours=6) for bar in bars)
    finally:
        conn.close()
