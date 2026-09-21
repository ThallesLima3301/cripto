"""Evaluate signals after their full 30-day measurement window.

A signal is available no earlier than both its reference candle's close
and its detection timestamp. Missing candles leave metrics NULL; later
maintenance fills them without losing known values when data is pruned.
Complete evaluations and unchanged retries are no-ops.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from crypto_monitor.config.settings import EvaluationSettings
from crypto_monitor.evaluation.common import (
    HOUR,
    is_complete,
    max_gain_loss_with_timing,
    price_at_horizon,
    save_evaluation,
)
from crypto_monitor.evaluation.verdict import assign_verdict
from crypto_monitor.utils.time_utils import from_utc_iso, now_utc


MATURATION_DAYS = 30
_REQUIRED_METRICS = (
    "price_24h_later", "price_7d_later", "price_30d_later",
    "return_24h_pct", "return_7d_pct", "return_30d_pct",
    "max_gain_7d_pct", "max_loss_7d_pct",
    "time_to_mfe_hours", "time_to_mae_hours",
)


@dataclass(frozen=True)
class SignalEvalResult:
    """Measured returns and excursions; missing coverage produces NULL.

    Excursion times are hours from signal availability to the opening of
    the extreme's hourly candle; ties use the earliest bar. Favorable
    excursion is nonnegative and adverse excursion nonpositive.
    """
    signal_id: int
    price_at_signal: float
    price_24h_later: float | None
    price_7d_later: float | None
    price_30d_later: float | None
    return_24h_pct: float | None
    return_7d_pct: float | None
    return_30d_pct: float | None
    max_gain_7d_pct: float | None
    max_loss_7d_pct: float | None
    verdict: str
    time_to_mfe_hours: float | None = None
    time_to_mae_hours: float | None = None


@dataclass(frozen=True)
class SignalEvalReport:
    """Missing/incomplete signals examined and changed evaluations."""
    considered: int
    evaluated: int
    skipped_pending: int  # examined but not yet matured


def evaluate_signal(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> SignalEvalResult | None:
    """Insert or complete one matured evaluation; return None on no change."""
    if now is None:
        now = now_utc()
    row = conn.execute(
        "SELECT id, symbol, candle_hour, detected_at, price_at_signal "
        "FROM signals WHERE id = ?", (signal_id,),
    ).fetchone()
    if row is None or is_complete(
        conn, "signal_evaluations", "signal_id", signal_id, _REQUIRED_METRICS,
    ):
        return None

    available_at = _available_at(row)
    if not _is_matured(available_at, now):
        return None
    result = _compute_signal_eval(
        conn, signal_id=signal_id, symbol=row["symbol"],
        available_at=available_at, price_at_signal=float(row["price_at_signal"]),
        eval_settings=eval_settings, now=now,
    )
    if not save_evaluation(
        conn, "signal_evaluations", "signal_id", result, now, eval_settings,
    ):
        return None
    conn.commit()
    stored = conn.execute(
        "SELECT * FROM signal_evaluations WHERE signal_id = ?", (signal_id,),
    ).fetchone()
    return SignalEvalResult(**{
        field.name: stored[field.name] for field in fields(SignalEvalResult)
    })


def evaluate_pending_signals(
    conn: sqlite3.Connection,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> SignalEvalReport:
    """Retry missing metrics on matured signals without duplicating rows."""
    if now is None:
        now = now_utc()
    missing = " OR ".join(f"e.{name} IS NULL" for name in _REQUIRED_METRICS)
    rows = conn.execute(
        "SELECT s.id, s.candle_hour, s.detected_at FROM signals s "
        "LEFT JOIN signal_evaluations e ON e.signal_id = s.id "
        f"WHERE e.signal_id IS NULL OR {missing} "
        "ORDER BY s.candle_hour ASC, s.id ASC",
    ).fetchall()
    evaluated = 0
    skipped_pending = 0
    for row in rows:
        if not _is_matured(_available_at(row), now):
            skipped_pending += 1
            continue
        if evaluate_signal(
            conn, int(row["id"]), eval_settings=eval_settings, now=now,
        ) is not None:
            evaluated += 1
    return SignalEvalReport(len(rows), evaluated, skipped_pending)


def _available_at(row: sqlite3.Row) -> datetime:
    return max(
        from_utc_iso(row["candle_hour"]) + HOUR,
        from_utc_iso(row["detected_at"]),
    )


def _is_matured(available_at: datetime, now: datetime) -> bool:
    return now >= available_at + timedelta(days=MATURATION_DAYS)


def _compute_signal_eval(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    available_at: datetime,
    price_at_signal: float,
    eval_settings: EvaluationSettings,
    now: datetime,
) -> SignalEvalResult:
    t_24h = available_at + timedelta(hours=24)
    t_7d = available_at + timedelta(days=7)
    t_30d = available_at + timedelta(days=30)
    price_24h = price_at_horizon(conn, symbol, t_24h, now=now)
    price_7d = price_at_horizon(conn, symbol, t_7d, now=now)
    price_30d = price_at_horizon(conn, symbol, t_30d, now=now)
    ret_24h = _pct_change(price_at_signal, price_24h)
    ret_7d = _pct_change(price_at_signal, price_7d)
    ret_30d = _pct_change(price_at_signal, price_30d)
    gain, loss, t_mfe, t_mae = max_gain_loss_with_timing(
        conn, symbol=symbol, start=available_at, end=t_7d, base=price_at_signal,
    )
    return SignalEvalResult(
        signal_id=signal_id, price_at_signal=price_at_signal,
        price_24h_later=price_24h, price_7d_later=price_7d,
        price_30d_later=price_30d, return_24h_pct=ret_24h,
        return_7d_pct=ret_7d, return_30d_pct=ret_30d,
        max_gain_7d_pct=gain, max_loss_7d_pct=loss,
        verdict=assign_verdict(ret_7d, eval_settings),
        time_to_mfe_hours=t_mfe, time_to_mae_hours=t_mae,
    )


def _pct_change(base: float, later: float | None) -> float | None:
    if later is None or base == 0:
        return None
    return (later - base) / base * 100.0
