"""Load recorded signals and hourly prices without modifying the database."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from crypto_monitor.paper.engine import simulate, validate_config
from crypto_monitor.paper.types import HourlyBar, PaperSignal, SimulationConfig, SimulationReport
from crypto_monitor.utils.time_utils import from_utc_iso, to_utc_iso


def simulate_database(
    db_path: Path | str,
    config: SimulationConfig,
    *,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> SimulationReport:
    """Replay one period from a consistent, read-only SQLite snapshot.

    This deliberately does not use the application's connection factory:
    paper analysis must never create a database, initialize or migrate it.
    """
    validate_config(config)
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Banco SQLite não encontrado: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        signals, bars = load_inputs(conn, start=start, end=end, config=config)
    finally:
        conn.close()
    return simulate(signals, bars, config, start=start, end=end, as_of=as_of)


def load_inputs(
    conn: sqlite3.Connection,
    *,
    start: datetime,
    end: datetime,
    config: SimulationConfig,
) -> tuple[list[PaperSignal], list[HourlyBar]]:
    """Read only candidates and prices relevant to this requested window.

    A signal detected before the window may still become available inside
    it after delivery retries or quiet hours. Select that case too. The
    engine makes the final availability/source/score decisions and counts
    undelivered signals rather than silently treating them as fills.
    """
    start_iso, end_iso = to_utc_iso(start), to_utc_iso(end)
    rows = conn.execute(
        """
        SELECT s.id, s.symbol, s.detected_at, s.candle_hour, s.score,
               (SELECT n.sent_at FROM notifications n
                WHERE n.signal_id = s.id AND n.delivered = 1
                  AND n.sent_at IS NOT NULL
                ORDER BY julianday(n.sent_at), n.id LIMIT 1) AS notified_at
        FROM signals s
        WHERE julianday(s.detected_at) < julianday(:end)
          AND julianday(s.candle_hour, '+1 hour') < julianday(:end)
          AND (julianday(s.detected_at) >= julianday(:start)
               OR julianday(s.candle_hour, '+1 hour') >= julianday(:start)
               OR EXISTS (
                   SELECT 1 FROM notifications n
                   WHERE n.signal_id = s.id AND n.delivered = 1
                     AND julianday(n.sent_at) >= julianday(:start)
                     AND julianday(n.sent_at) < julianday(:end)))
        ORDER BY s.id
        """,
        {"start": start_iso, "end": end_iso},
    ).fetchall()
    signals = [
        PaperSignal(
            signal_id=row["id"],
            symbol=row["symbol"],
            detected_at=from_utc_iso(row["detected_at"]),
            candle_hour=from_utc_iso(row["candle_hour"]),
            score=row["score"],
            notified_at=(from_utc_iso(row["notified_at"]) if row["notified_at"] else None),
        )
        for row in rows
    ]
    symbols = sorted({config.benchmark_symbol, *(signal.symbol for signal in signals)})
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""
        SELECT symbol, open_time, open, close FROM candles
        WHERE interval = '1h' AND symbol IN ({placeholders})
          AND julianday(open_time) >= julianday(?)
          AND julianday(open_time) < julianday(?)
        ORDER BY open_time, symbol
        """,
        (*symbols, start_iso, end_iso),
    ).fetchall()
    bars = [
        HourlyBar(
            symbol=row["symbol"],
            open_time=from_utc_iso(row["open_time"]),
            open=row["open"],
            close=row["close"],
        )
        for row in rows
    ]
    return signals, bars
