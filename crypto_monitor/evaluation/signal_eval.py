"""Matured signal evaluation.

For every signal row that is at least `MATURATION_DAYS` old and has
no row in `signal_evaluations`, we compute 24h / 7d / 30d returns
and max-gain/max-loss over the 7-day window, assign a verdict, and
insert a row.

Evaluation anchors off `candle_hour` (the signal's reference 1h
open_time) rather than `detected_at`, because `price_at_signal` is
tied to that candle's close. Future price lookups then use 1h
candles — the same granularity the scoring engine already ingests —
which is plenty of resolution for 24h/7d/30d comparisons.

Maturation is strict: we only evaluate once the full 30-day window
has elapsed. That makes each evaluation one-shot and lets the
`UNIQUE(signal_id)` constraint on `signal_evaluations` act as the
idempotency guard if the evaluator runs twice in a day.

Not-matured signals are left alone — they appear in the next
evaluation run once enough time has passed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from crypto_monitor.config.settings import EvaluationSettings
from crypto_monitor.evaluation.verdict import VERDICT_PENDING, assign_verdict
from crypto_monitor.utils.time_utils import (
    from_utc_iso,
    now_utc,
    to_utc_iso,
)


logger = logging.getLogger(__name__)


MATURATION_DAYS = 30


@dataclass(frozen=True)
class SignalEvalResult:
    """Outcome of evaluating a single matured signal.

    All percent fields are absolute percent differences, signed:
    positive = up, negative = down. `max_gain_7d_pct` is always
    >= 0 and `max_loss_7d_pct` is always <= 0.
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


@dataclass(frozen=True)
class SignalEvalReport:
    """Summary of an `evaluate_pending_signals` run."""
    considered: int       # signals examined (matured AND unevaluated)
    evaluated: int        # rows actually written
    skipped_pending: int  # examined but not yet matured


# ---------- public API ----------

def evaluate_signal(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> SignalEvalResult | None:
    """Evaluate a single signal by id.

    Returns `None` if the signal does not exist, is not yet matured,
    or already has a `signal_evaluations` row. Otherwise computes
    the result, inserts it, and returns it.
    """
    if now is None:
        now = now_utc()

    row = conn.execute(
        """
        SELECT id, symbol, candle_hour, price_at_signal
        FROM signals
        WHERE id = ?
        """,
        (signal_id,),
    ).fetchone()
    if row is None:
        return None

    if _already_evaluated(conn, signal_id):
        return None

    candle_anchor = from_utc_iso(row["candle_hour"])
    if not _is_matured(candle_anchor, now):
        return None

    result = _compute_signal_eval(
        conn,
        signal_id=int(row["id"]),
        symbol=row["symbol"],
        candle_anchor=candle_anchor,
        price_at_signal=float(row["price_at_signal"]),
        eval_settings=eval_settings,
    )
    _insert_signal_evaluation(conn, result, now)
    conn.commit()
    return result


def evaluate_pending_signals(
    conn: sqlite3.Connection,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> SignalEvalReport:
    """Walk every un-evaluated signal and evaluate matured ones."""
    if now is None:
        now = now_utc()

    rows = conn.execute(
        """
        SELECT s.id, s.symbol, s.candle_hour, s.price_at_signal
        FROM signals s
        LEFT JOIN signal_evaluations e ON e.signal_id = s.id
        WHERE e.signal_id IS NULL
        ORDER BY s.candle_hour ASC, s.id ASC
        """
    ).fetchall()

    considered = len(rows)
    evaluated = 0
    skipped_pending = 0

    for row in rows:
        candle_anchor = from_utc_iso(row["candle_hour"])
        if not _is_matured(candle_anchor, now):
            skipped_pending += 1
            continue

        result = _compute_signal_eval(
            conn,
            signal_id=int(row["id"]),
            symbol=row["symbol"],
            candle_anchor=candle_anchor,
            price_at_signal=float(row["price_at_signal"]),
            eval_settings=eval_settings,
        )
        _insert_signal_evaluation(conn, result, now)
        evaluated += 1

    conn.commit()
    return SignalEvalReport(
        considered=considered,
        evaluated=evaluated,
        skipped_pending=skipped_pending,
    )


# ---------- internals ----------

def _is_matured(candle_anchor: datetime, now: datetime) -> bool:
    return now >= candle_anchor + timedelta(days=MATURATION_DAYS)


def _already_evaluated(conn: sqlite3.Connection, signal_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM signal_evaluations WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        is not None
    )


def _compute_signal_eval(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    candle_anchor: datetime,
    price_at_signal: float,
    eval_settings: EvaluationSettings,
) -> SignalEvalResult:
    """Compute all return fields for a matured signal.

    Every price lookup uses 1h candles so 24h / 7d / 30d comparisons
    draw from the same granularity the scoring engine already has.
    """
    t_24h = candle_anchor + timedelta(hours=24)
    t_7d = candle_anchor + timedelta(days=7)
    t_30d = candle_anchor + timedelta(days=30)

    price_24h = _price_at_or_after(conn, symbol, t_24h)
    price_7d = _price_at_or_after(conn, symbol, t_7d)
    price_30d = _price_at_or_after(conn, symbol, t_30d)

    ret_24h = _pct_change(price_at_signal, price_24h)
    ret_7d = _pct_change(price_at_signal, price_7d)
    ret_30d = _pct_change(price_at_signal, price_30d)

    max_gain_pct, max_loss_pct = _max_gain_loss_window(
        conn,
        symbol=symbol,
        start=candle_anchor,
        end=t_7d,
        base=price_at_signal,
    )

    verdict = assign_verdict(ret_7d, eval_settings)

    return SignalEvalResult(
        signal_id=signal_id,
        price_at_signal=price_at_signal,
        price_24h_later=price_24h,
        price_7d_later=price_7d,
        price_30d_later=price_30d,
        return_24h_pct=ret_24h,
        return_7d_pct=ret_7d,
        return_30d_pct=ret_30d,
        max_gain_7d_pct=max_gain_pct,
        max_loss_7d_pct=max_loss_pct,
        verdict=verdict,
    )


def _price_at_or_after(
    conn: sqlite3.Connection,
    symbol: str,
    target: datetime,
    interval: str = "1h",
) -> float | None:
    """Return the close of the first 1h candle whose open_time >= target.

    Using "at or after" (rather than "the last candle before target")
    gives a conservative estimate: when the requested moment falls
    between two candles, we use the one that fully contains the
    target time.
    """
    target_iso = to_utc_iso(target)
    row = conn.execute(
        """
        SELECT close FROM candles
        WHERE symbol = ? AND interval = ? AND open_time >= ?
        ORDER BY open_time ASC
        LIMIT 1
        """,
        (symbol, interval, target_iso),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


def _max_gain_loss_window(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    base: float,
    interval: str = "1h",
) -> tuple[float | None, float | None]:
    """Return (max_gain_pct, max_loss_pct) over [start, end] inclusive.

    Uses the max HIGH and min LOW across the 1h candles in the
    window, which matches the wording we promised: hourly-resolution
    high and low, not tick-level.
    """
    start_iso = to_utc_iso(start)
    end_iso = to_utc_iso(end)
    row = conn.execute(
        """
        SELECT MAX(high) AS hi, MIN(low) AS lo
        FROM candles
        WHERE symbol = ? AND interval = ?
          AND open_time >= ? AND open_time <= ?
        """,
        (symbol, interval, start_iso, end_iso),
    ).fetchone()
    if row is None or row["hi"] is None or row["lo"] is None:
        return (None, None)
    hi = float(row["hi"])
    lo = float(row["lo"])
    max_gain_pct = (hi - base) / base * 100.0
    max_loss_pct = (lo - base) / base * 100.0
    return (max_gain_pct, max_loss_pct)


def _pct_change(base: float, later: float | None) -> float | None:
    if later is None:
        return None
    if base == 0:
        return None
    return (later - base) / base * 100.0


def _insert_signal_evaluation(
    conn: sqlite3.Connection,
    r: SignalEvalResult,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO signal_evaluations (
            signal_id, evaluated_at, price_at_signal,
            price_24h_later, price_7d_later, price_30d_later,
            return_24h_pct, return_7d_pct, return_30d_pct,
            max_gain_7d_pct, max_loss_7d_pct, verdict
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r.signal_id,
            to_utc_iso(now),
            r.price_at_signal,
            r.price_24h_later,
            r.price_7d_later,
            r.price_30d_later,
            r.return_24h_pct,
            r.return_7d_pct,
            r.return_30d_pct,
            r.max_gain_7d_pct,
            r.max_loss_7d_pct,
            r.verdict,
        ),
    )
