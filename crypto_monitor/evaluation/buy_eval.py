"""Matured buy evaluation.

For every buy row that is at least `MATURATION_DAYS` old and has no
companion row in `buy_evaluations`, compute:

  * the buy day's 1h OPEN  (`day_open`)
  * the buy day's hourly LOW — the minimum of `low` across the 1h
    candles inside the buy's UTC day (`day_low_hourly`)
  * the open_time of the 1h candle that hit that low
    (`day_low_hourly_time`)
  * three descriptive percents:
        pct_from_day_open_to_low_hourly   = (low - day_open) / day_open * 100
        pct_from_buy_to_low_hourly        = (low - buy_price) / buy_price * 100
        buy_vs_day_low_hourly_pct         = (buy_price - low) / low * 100
  * 7d and 30d later closes + their percent returns vs. `buy_price`
  * a verdict assigned from the 7-day return via
    `assign_verdict`.

Hourly wording
--------------
The spec is explicit: this is an HOURLY-resolution intraday low,
not a tick-level one. We compute it from the `candles` table whose
smallest interval is 1h. `resolution_note` on the evaluation row
records that wording so a report reader never has to guess.

Separation of concerns
----------------------
`compute_day_low_hourly` is a pure function over a list of
`Candle` — no DB, no wall clock. The DB wrapper
`evaluate_buy` loads the right candles and calls it. Tests can
exercise the pure helper against synthetic candle lists without
touching SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from crypto_monitor.config.settings import EvaluationSettings
from crypto_monitor.evaluation.verdict import assign_verdict
from crypto_monitor.evaluation.signal_eval import (
    MATURATION_DAYS,
    _max_gain_loss_with_timing,
    _pct_change,
    _price_at_or_after,
)
from crypto_monitor.indicators import Candle
from crypto_monitor.utils.time_utils import (
    floor_to_day,
    from_utc_iso,
    now_utc,
    to_utc_iso,
)


logger = logging.getLogger(__name__)


_RESOLUTION_NOTE = (
    "hourly-resolution intraday low — computed from 1h candle lows, "
    "not tick-level"
)


# ---------- pure helper ----------

@dataclass(frozen=True)
class DayLowResult:
    """Output of `compute_day_low_hourly`.

    `day_open` is the open of the first 1h candle in the day.
    `day_low_hourly` is the min `low` across all 1h candles in the day.
    `day_low_hourly_time` is the `open_time` of the candle that hit that low.
    """
    day_open: float
    day_low_hourly: float
    day_low_hourly_time: str


def compute_day_low_hourly(candles: list[Candle]) -> DayLowResult | None:
    """Compute the day open and hourly low from a list of 1h candles.

    The caller is responsible for filtering the list down to a
    single UTC day, chronologically ordered. Returns `None` if the
    list is empty — the buy's day has no candles we can use.

    Ties on `low` are broken by the EARLIEST open_time (we prefer
    the first occurrence of the minimum so a downstream "time of
    day low" read stays deterministic).
    """
    if not candles:
        return None

    day_open = candles[0].open
    min_low = candles[0].low
    min_time = candles[0].open_time
    for c in candles[1:]:
        if c.low < min_low:
            min_low = c.low
            min_time = c.open_time

    return DayLowResult(
        day_open=day_open,
        day_low_hourly=min_low,
        day_low_hourly_time=min_time,
    )


# ---------- DB-facing result + report ----------

@dataclass(frozen=True)
class BuyEvalResult:
    buy_id: int
    day_open: float | None
    day_low_hourly: float | None
    day_low_hourly_time: str | None
    pct_from_day_open_to_low_hourly: float | None
    pct_from_buy_to_low_hourly: float | None
    buy_vs_day_low_hourly_pct: float | None
    price_7d_later: float | None
    return_7d_pct: float | None
    price_30d_later: float | None
    return_30d_pct: float | None
    verdict: str
    resolution_note: str
    # ---- Block 24: MFE/MAE over the 7-day post-buy window ----
    max_gain_pct: float | None = None
    max_loss_pct: float | None = None
    time_to_mfe_hours: float | None = None
    time_to_mae_hours: float | None = None


@dataclass(frozen=True)
class BuyEvalReport:
    considered: int
    evaluated: int
    skipped_pending: int


# ---------- public API ----------

def evaluate_buy(
    conn: sqlite3.Connection,
    buy_id: int,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> BuyEvalResult | None:
    """Evaluate a single matured buy. Returns None if not matured or missing.

    Behaves symmetrically to `evaluate_signal`: skips buys that do
    not exist, that are already evaluated, or that are too young.
    """
    if now is None:
        now = now_utc()

    row = conn.execute(
        """
        SELECT id, symbol, bought_at, price
        FROM buys
        WHERE id = ?
        """,
        (buy_id,),
    ).fetchone()
    if row is None:
        return None

    if _already_evaluated(conn, buy_id):
        return None

    bought_at = from_utc_iso(row["bought_at"])
    if not _is_matured(bought_at, now):
        return None

    result = _compute_buy_eval(
        conn,
        buy_id=int(row["id"]),
        symbol=row["symbol"],
        bought_at=bought_at,
        buy_price=float(row["price"]),
        eval_settings=eval_settings,
    )
    _insert_buy_evaluation(conn, result, now)
    conn.commit()
    return result


def evaluate_pending_buys(
    conn: sqlite3.Connection,
    *,
    eval_settings: EvaluationSettings,
    now: datetime | None = None,
) -> BuyEvalReport:
    """Walk every un-evaluated buy and evaluate matured ones."""
    if now is None:
        now = now_utc()

    rows = conn.execute(
        """
        SELECT b.id, b.symbol, b.bought_at, b.price
        FROM buys b
        LEFT JOIN buy_evaluations e ON e.buy_id = b.id
        WHERE e.buy_id IS NULL
        ORDER BY b.bought_at ASC, b.id ASC
        """
    ).fetchall()

    considered = len(rows)
    evaluated = 0
    skipped_pending = 0

    for row in rows:
        bought_at = from_utc_iso(row["bought_at"])
        if not _is_matured(bought_at, now):
            skipped_pending += 1
            continue

        result = _compute_buy_eval(
            conn,
            buy_id=int(row["id"]),
            symbol=row["symbol"],
            bought_at=bought_at,
            buy_price=float(row["price"]),
            eval_settings=eval_settings,
        )
        _insert_buy_evaluation(conn, result, now)
        evaluated += 1

    conn.commit()
    return BuyEvalReport(
        considered=considered,
        evaluated=evaluated,
        skipped_pending=skipped_pending,
    )


# ---------- internals ----------

def _is_matured(bought_at: datetime, now: datetime) -> bool:
    return now >= bought_at + timedelta(days=MATURATION_DAYS)


def _already_evaluated(conn: sqlite3.Connection, buy_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM buy_evaluations WHERE buy_id = ?",
            (buy_id,),
        ).fetchone()
        is not None
    )


def _compute_buy_eval(
    conn: sqlite3.Connection,
    *,
    buy_id: int,
    symbol: str,
    bought_at: datetime,
    buy_price: float,
    eval_settings: EvaluationSettings,
) -> BuyEvalResult:
    # --- intraday low via pure helper ---
    day_candles = _load_day_candles(conn, symbol, bought_at)
    day_low = compute_day_low_hourly(day_candles)

    if day_low is not None:
        day_open = day_low.day_open
        day_low_hourly = day_low.day_low_hourly
        day_low_hourly_time = day_low.day_low_hourly_time
        pct_from_day_open_to_low = (
            (day_low_hourly - day_open) / day_open * 100.0
            if day_open
            else None
        )
        pct_from_buy_to_low = (
            (day_low_hourly - buy_price) / buy_price * 100.0
            if buy_price
            else None
        )
        buy_vs_day_low_pct = (
            (buy_price - day_low_hourly) / day_low_hourly * 100.0
            if day_low_hourly
            else None
        )
    else:
        day_open = None
        day_low_hourly = None
        day_low_hourly_time = None
        pct_from_day_open_to_low = None
        pct_from_buy_to_low = None
        buy_vs_day_low_pct = None

    # --- 7d / 30d returns ---
    t_7d = bought_at + timedelta(days=7)
    t_30d = bought_at + timedelta(days=30)
    price_7d = _price_at_or_after(conn, symbol, t_7d)
    price_30d = _price_at_or_after(conn, symbol, t_30d)

    return_7d = _pct_change(buy_price, price_7d)
    return_30d = _pct_change(buy_price, price_30d)

    # --- Block 24: MFE / MAE + timing over the 7-day post-buy window ---
    max_gain_pct, max_loss_pct, t_to_mfe, t_to_mae = _max_gain_loss_with_timing(
        conn,
        symbol=symbol,
        start=bought_at,
        end=t_7d,
        base=buy_price,
    )

    verdict = assign_verdict(return_7d, eval_settings)

    return BuyEvalResult(
        buy_id=buy_id,
        day_open=day_open,
        day_low_hourly=day_low_hourly,
        day_low_hourly_time=day_low_hourly_time,
        pct_from_day_open_to_low_hourly=pct_from_day_open_to_low,
        pct_from_buy_to_low_hourly=pct_from_buy_to_low,
        buy_vs_day_low_hourly_pct=buy_vs_day_low_pct,
        price_7d_later=price_7d,
        return_7d_pct=return_7d,
        price_30d_later=price_30d,
        return_30d_pct=return_30d,
        verdict=verdict,
        resolution_note=_RESOLUTION_NOTE,
        max_gain_pct=max_gain_pct,
        max_loss_pct=max_loss_pct,
        time_to_mfe_hours=t_to_mfe,
        time_to_mae_hours=t_to_mae,
    )


def _load_day_candles(
    conn: sqlite3.Connection,
    symbol: str,
    bought_at: datetime,
) -> list[Candle]:
    """Load the 1h candles that fall inside the UTC day of `bought_at`."""
    day_start = floor_to_day(bought_at)
    day_end = day_start + timedelta(days=1)
    rows = conn.execute(
        """
        SELECT open_time, open, high, low, close, volume, close_time
        FROM candles
        WHERE symbol = ? AND interval = '1h'
          AND open_time >= ? AND open_time < ?
        ORDER BY open_time ASC
        """,
        (symbol, to_utc_iso(day_start), to_utc_iso(day_end)),
    ).fetchall()
    return [
        Candle(
            open_time=r["open_time"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            close_time=r["close_time"],
        )
        for r in rows
    ]


def _insert_buy_evaluation(
    conn: sqlite3.Connection,
    r: BuyEvalResult,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO buy_evaluations (
            buy_id, evaluated_at, day_open,
            day_low_hourly, day_low_hourly_time,
            pct_from_day_open_to_low_hourly,
            pct_from_buy_to_low_hourly,
            buy_vs_day_low_hourly_pct,
            price_7d_later, return_7d_pct,
            price_30d_later, return_30d_pct,
            verdict, resolution_note,
            max_gain_pct, max_loss_pct,
            time_to_mfe_hours, time_to_mae_hours
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r.buy_id,
            to_utc_iso(now),
            r.day_open,
            r.day_low_hourly,
            r.day_low_hourly_time,
            r.pct_from_day_open_to_low_hourly,
            r.pct_from_buy_to_low_hourly,
            r.buy_vs_day_low_hourly_pct,
            r.price_7d_later,
            r.return_7d_pct,
            r.price_30d_later,
            r.return_30d_pct,
            r.verdict,
            r.resolution_note,
            r.max_gain_pct,
            r.max_loss_pct,
            r.time_to_mfe_hours,
            r.time_to_mae_hours,
        ),
    )
