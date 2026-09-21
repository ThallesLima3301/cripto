"""Hourly windows and persistence shared by the signal and buy evaluators.

Candle availability is its opening plus one hour, normalizing Binance's
last-millisecond close timestamp to the scheduler's hourly boundary.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta

from crypto_monitor.config.settings import EvaluationSettings
from crypto_monitor.evaluation.verdict import assign_verdict
from crypto_monitor.utils.time_utils import floor_to_hour, from_utc_iso, to_utc_iso


HOUR = timedelta(hours=1)
_EVALUATION_KEYS = {
    "signal_evaluations": "signal_id",
    "buy_evaluations": "buy_id",
}


def price_at_horizon(
    conn: sqlite3.Connection,
    symbol: str,
    target: datetime,
    *,
    now: datetime,
) -> float | None:
    """First hourly close at/after target, less than one hour later.

    At 15:00 use the 14:00 candle's close; at 15:05 use the 15:00
    candle's close, available at 16:00. If that exact candle is missing
    or not yet closed at ``now``, the horizon remains unknown.
    """
    opening = floor_to_hour(target)
    if opening == target:
        opening -= HOUR
    if opening + HOUR > now:
        return None
    row = conn.execute(
        "SELECT close FROM candles "
        "WHERE symbol = ? AND interval = '1h' AND open_time = ?",
        (symbol, to_utc_iso(opening)),
    ).fetchone()
    return float(row["close"]) if row is not None else None


def max_gain_loss_with_timing(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    base: float,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Excursions over a complete set of fully contained hourly bars.

    Exclude bars crossing either boundary. Every remaining hour must
    be present or all metrics stay pending. Timing is the extreme's
    candle opening relative to ``start``, not an observed tick time.
    Equal extremes use the earliest candle. Zero excursion uses the
    entry baseline at time zero.
    """
    first = floor_to_hour(start)
    if first < start:
        first += HOUR
    stop = floor_to_hour(end)
    expected = int((stop - first) / HOUR)
    if expected <= 0 or base <= 0:
        return (None, None, None, None)
    rows = conn.execute(
        "SELECT open_time, high, low FROM candles "
        "WHERE symbol = ? AND interval = '1h' "
        "AND open_time >= ? AND open_time < ? ORDER BY open_time ASC",
        (symbol, to_utc_iso(first), to_utc_iso(stop)),
    ).fetchall()
    if len(rows) != expected or any(
        from_utc_iso(row["open_time"]) != first + i * HOUR
        for i, row in enumerate(rows)
    ):
        return (None, None, None, None)
    high_row = max(rows, key=lambda row: float(row["high"]))
    low_row = min(rows, key=lambda row: float(row["low"]))
    gain = max((float(high_row["high"]) - base) / base * 100.0, 0.0)
    loss = min((float(low_row["low"]) - base) / base * 100.0, 0.0)
    gain_hours = (
        (from_utc_iso(high_row["open_time"]) - start).total_seconds() / 3600.0
        if gain > 0 else 0.0
    )
    loss_hours = (
        (from_utc_iso(low_row["open_time"]) - start).total_seconds() / 3600.0
        if loss < 0 else 0.0
    )
    return gain, loss, gain_hours, loss_hours


def is_complete(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    key: int,
    fields: tuple[str, ...],
) -> bool:
    """Whether all required metrics have already been measured."""
    _validate_table(table, key_column)
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {key_column} = ?", (key,),
    ).fetchone()
    return row is not None and all(row[field] is not None for field in fields)


def save_evaluation(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    result,
    now: datetime,
    eval_settings: EvaluationSettings,
) -> bool:
    """Upsert metrics, preserving known values when candles were pruned.

    Migration 6 archives legacy calculations before using these tables.
    Unchanged retries do not write or move ``evaluated_at``. The caller
    owns the transaction and reads the merged row for its return value.
    """
    _validate_table(table, key_column)
    values = asdict(result)
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {key_column} = ?", (values[key_column],),
    ).fetchone()
    if existing is not None:
        values = {
            name: existing[name] if value is None else value
            for name, value in values.items()
        }
    values["verdict"] = assign_verdict(values["return_7d_pct"], eval_settings)
    if existing is not None and all(existing[name] == value for name, value in values.items()):
        return False
    values["evaluated_at"] = to_utc_iso(now)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in values if name != key_column
    )
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT({key_column}) DO UPDATE SET {updates}",
        tuple(values.values()),
    )
    return True


def _validate_table(table: str, key_column: str) -> None:
    if _EVALUATION_KEYS.get(table) != key_column:
        raise ValueError("unsupported evaluation table/key")
