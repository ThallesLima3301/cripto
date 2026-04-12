"""Tests for `crypto_monitor.evaluation.buy_eval`.

Covers:
  * the pure `compute_day_low_hourly` helper on synthetic candles
    (including tie-breaking and empty-list behavior)
  * matured buy evaluation end-to-end (intraday low fields, 7d/30d
    returns, verdict)
  * pending / not-enough-future-data behavior
  * idempotent reruns
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto_monitor.buys import insert_buy
from crypto_monitor.evaluation import (
    VERDICT_BAD,
    VERDICT_GOOD,
    VERDICT_PENDING,
    compute_day_low_hourly,
    evaluate_buy,
    evaluate_pending_buys,
)
from crypto_monitor.indicators import Candle


UTC = timezone.utc


# ---------- pure helper ----------

def _mk(open_time: str, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        open_time=open_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        close_time=open_time,
    )


def test_compute_day_low_hourly_basic():
    # 3 candles on the same UTC day. The second hits the minimum low.
    candles = [
        _mk("2026-04-01T00:00:00Z", 100.0, 101.0, 99.0, 100.5),
        _mk("2026-04-01T01:00:00Z", 100.5, 101.0, 96.0, 97.0),   # min low
        _mk("2026-04-01T02:00:00Z", 97.0, 98.0, 96.5, 97.5),
    ]
    result = compute_day_low_hourly(candles)
    assert result is not None
    assert result.day_open == 100.0
    assert result.day_low_hourly == 96.0
    assert result.day_low_hourly_time == "2026-04-01T01:00:00Z"


def test_compute_day_low_hourly_ties_prefer_earliest():
    candles = [
        _mk("2026-04-01T00:00:00Z", 100.0, 100.0, 95.0, 99.0),   # first tie
        _mk("2026-04-01T01:00:00Z", 99.0, 100.0, 95.0, 99.5),    # later tie
        _mk("2026-04-01T02:00:00Z", 99.5, 100.0, 96.0, 99.5),
    ]
    result = compute_day_low_hourly(candles)
    assert result is not None
    assert result.day_low_hourly == 95.0
    # Earliest tie wins so downstream "time of day low" is deterministic.
    assert result.day_low_hourly_time == "2026-04-01T00:00:00Z"


def test_compute_day_low_hourly_empty_returns_none():
    assert compute_day_low_hourly([]) is None


# ---------- DB helpers ----------

def _insert_candle_row(
    conn,
    *,
    symbol: str,
    open_time: datetime,
    open_: float,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    interval: str = "1h",
) -> None:
    o = open_
    h = high if high is not None else open_
    l = low if low is not None else open_
    c = close if close is not None else open_
    conn.execute(
        """
        INSERT INTO candles
            (symbol, interval, open_time, open, high, low, close, volume, close_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, 100.0, ?)
        """,
        (
            symbol, interval,
            open_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            o, h, l, c,
            (open_time + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def _seed_buy_day_and_future(
    conn,
    *,
    symbol: str,
    day_start: datetime,
    bought_at: datetime,
    hourly_lows: list[float],
    hourly_opens: list[float],
    price_7d: float,
    price_30d: float,
) -> None:
    """Write 24 1h candles for the buy's day plus candles at
    `bought_at + 7d` and `bought_at + 30d` (the timestamps the
    evaluator actually looks up)."""
    assert len(hourly_lows) == 24
    assert len(hourly_opens) == 24
    for i in range(24):
        _insert_candle_row(
            conn,
            symbol=symbol,
            open_time=day_start + timedelta(hours=i),
            open_=hourly_opens[i],
            high=max(hourly_opens[i], hourly_lows[i]) + 1.0,
            low=hourly_lows[i],
            close=hourly_opens[i],
        )
    _insert_candle_row(
        conn, symbol=symbol,
        open_time=bought_at + timedelta(days=7),
        open_=price_7d,
    )
    _insert_candle_row(
        conn, symbol=symbol,
        open_time=bought_at + timedelta(days=30),
        open_=price_30d,
    )
    conn.commit()


# ---------- matured buy evaluation ----------

def test_matured_buy_evaluation_end_to_end(memory_db, eval_settings):
    day_start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    buy_time = day_start + timedelta(hours=12)  # bought mid-day

    # Day candles: opens flat at 100, low dips to 90 at hour 3 and recovers.
    hourly_opens = [100.0] * 24
    hourly_lows = [98.0] * 24
    hourly_lows[3] = 90.0  # THE hourly low of the day
    _seed_buy_day_and_future(
        memory_db,
        symbol="BTCUSDT",
        day_start=day_start,
        bought_at=buy_time,
        hourly_lows=hourly_lows,
        hourly_opens=hourly_opens,
        price_7d=108.0,   # +8% vs buy_price=100 → good
        price_30d=95.0,   # -5%  (doesn't drive verdict; 7d does)
    )

    buy = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=buy_time,
        price=100.0,
        amount_invested=1000.0,
        now=buy_time,
    )

    now = buy_time + timedelta(days=31)  # matured
    result = evaluate_buy(
        memory_db, buy.id, eval_settings=eval_settings, now=now
    )

    assert result is not None
    assert result.buy_id == buy.id
    assert result.day_open == 100.0
    assert result.day_low_hourly == 90.0
    assert result.day_low_hourly_time == "2026-03-01T03:00:00Z"

    # (90 - 100) / 100 * 100 = -10
    assert result.pct_from_day_open_to_low_hourly == pytest.approx(-10.0)
    # buy was at 100, day low 90 → same -10 since buy == day_open here
    assert result.pct_from_buy_to_low_hourly == pytest.approx(-10.0)
    # (100 - 90) / 90 * 100 = ~11.11 — we bought 11% above the day's hourly low
    assert result.buy_vs_day_low_hourly_pct == pytest.approx(100.0 / 9.0)

    assert result.price_7d_later == 108.0
    assert result.return_7d_pct == pytest.approx(8.0)
    assert result.price_30d_later == 95.0
    assert result.return_30d_pct == pytest.approx(-5.0)
    assert result.verdict == VERDICT_GOOD
    assert "hourly-resolution" in result.resolution_note

    # Persisted row matches.
    row = memory_db.execute(
        "SELECT * FROM buy_evaluations WHERE buy_id = ?", (buy.id,)
    ).fetchone()
    assert row is not None
    assert row["day_low_hourly"] == 90.0
    assert row["verdict"] == VERDICT_GOOD


def test_matured_buy_bad_verdict(memory_db, eval_settings):
    day_start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    buy_time = day_start + timedelta(hours=12)
    hourly_opens = [50.0] * 24
    hourly_lows = [49.0] * 24
    _seed_buy_day_and_future(
        memory_db,
        symbol="ETHUSDT",
        day_start=day_start,
        bought_at=buy_time,
        hourly_lows=hourly_lows,
        hourly_opens=hourly_opens,
        price_7d=44.0,    # -12% vs buy_price=50 → bad
        price_30d=40.0,   # -20%
    )
    buy = insert_buy(
        memory_db,
        symbol="ETHUSDT",
        bought_at=buy_time,
        price=50.0,
        amount_invested=500.0,
        now=buy_time,
    )

    now = buy_time + timedelta(days=31)
    result = evaluate_buy(
        memory_db, buy.id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.return_7d_pct == pytest.approx(-12.0)
    assert result.verdict == VERDICT_BAD


# ---------- pending / not-enough-future-data ----------

def test_buy_too_young_is_pending(memory_db, eval_settings):
    day_start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    buy = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=day_start + timedelta(hours=12),
        price=100.0,
        amount_invested=1000.0,
        now=day_start + timedelta(hours=12),
    )

    now = day_start + timedelta(days=10)  # only 10 days — not matured
    result = evaluate_buy(
        memory_db, buy.id, eval_settings=eval_settings, now=now
    )
    assert result is None
    assert memory_db.execute(
        "SELECT 1 FROM buy_evaluations WHERE buy_id = ?", (buy.id,)
    ).fetchone() is None


def test_matured_buy_with_no_future_candles_has_pending_verdict(
    memory_db, eval_settings
):
    day_start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    buy_time = day_start + timedelta(hours=12)
    # Only seed the day candles — no +7d / +30d candles.
    for i in range(24):
        _insert_candle_row(
            memory_db,
            symbol="BTCUSDT",
            open_time=day_start + timedelta(hours=i),
            open_=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
    memory_db.commit()

    buy = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=buy_time,
        price=100.0,
        amount_invested=1000.0,
        now=buy_time,
    )
    now = buy_time + timedelta(days=31)
    result = evaluate_buy(memory_db, buy.id, eval_settings=eval_settings, now=now)

    assert result is not None
    # Day fields still populated — we had the same-day candles.
    assert result.day_open == 100.0
    assert result.day_low_hourly == 99.0
    # Future lookups missed → None → pending verdict.
    assert result.price_7d_later is None
    assert result.price_30d_later is None
    assert result.return_7d_pct is None
    assert result.verdict == VERDICT_PENDING


def test_evaluate_pending_buys_skips_young_and_evaluates_mature(
    memory_db, eval_settings
):
    # One mature buy (past 30d + future candles).
    mature_day = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    mature_buy_time = mature_day + timedelta(hours=12)
    _seed_buy_day_and_future(
        memory_db,
        symbol="BTCUSDT",
        day_start=mature_day,
        bought_at=mature_buy_time,
        hourly_lows=[99.0] * 24,
        hourly_opens=[100.0] * 24,
        price_7d=108.0,
        price_30d=110.0,
    )
    mature_buy = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=mature_buy_time,
        price=100.0,
        amount_invested=1000.0,
        now=mature_buy_time,
    )

    # One young buy — not matured.
    young_buy_time = mature_day + timedelta(days=25)
    young_buy = insert_buy(
        memory_db,
        symbol="ETHUSDT",
        bought_at=young_buy_time,
        price=2000.0,
        amount_invested=2000.0,
        now=young_buy_time,
    )

    now = mature_day + timedelta(days=31)
    report = evaluate_pending_buys(
        memory_db, eval_settings=eval_settings, now=now
    )

    assert report.considered == 2
    assert report.evaluated == 1
    assert report.skipped_pending == 1

    mature_row = memory_db.execute(
        "SELECT 1 FROM buy_evaluations WHERE buy_id = ?", (mature_buy.id,)
    ).fetchone()
    young_row = memory_db.execute(
        "SELECT 1 FROM buy_evaluations WHERE buy_id = ?", (young_buy.id,)
    ).fetchone()
    assert mature_row is not None
    assert young_row is None


# ---------- idempotency ----------

def test_rerunning_buy_evaluation_is_a_noop(memory_db, eval_settings):
    day_start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    buy_time = day_start + timedelta(hours=12)
    _seed_buy_day_and_future(
        memory_db,
        symbol="BTCUSDT",
        day_start=day_start,
        bought_at=buy_time,
        hourly_lows=[99.0] * 24,
        hourly_opens=[100.0] * 24,
        price_7d=108.0,
        price_30d=110.0,
    )
    buy = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=buy_time,
        price=100.0,
        amount_invested=1000.0,
        now=buy_time,
    )

    now = buy_time + timedelta(days=31)
    first = evaluate_buy(memory_db, buy.id, eval_settings=eval_settings, now=now)
    second = evaluate_buy(memory_db, buy.id, eval_settings=eval_settings, now=now)
    assert first is not None
    assert second is None

    assert memory_db.execute(
        "SELECT COUNT(*) FROM buy_evaluations WHERE buy_id = ?", (buy.id,)
    ).fetchone()[0] == 1
