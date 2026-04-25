"""Tests for `crypto_monitor.evaluation.signal_eval`.

Covers:
  * matured signal evaluation end-to-end (24h/7d/30d returns,
    max_gain/max_loss over the 7d window, verdict assignment, row
    inserted into signal_evaluations)
  * pending / not-enough-future-data behavior (signal too young →
    no row written, reported as skipped_pending)
  * rerun idempotency (UNIQUE(signal_id) means a second call is a no-op)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto_monitor.evaluation import (
    VERDICT_GOOD,
    VERDICT_GREAT,
    VERDICT_PENDING,
    evaluate_pending_signals,
    evaluate_signal,
)


UTC = timezone.utc


# ---------- helpers ----------

def _insert_signal(
    conn,
    *,
    symbol: str = "BTCUSDT",
    candle_hour: str = "2026-03-01T14:00:00Z",
    price: float = 40.0,
    score: int = 72,
    severity: str = "strong",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown
        ) VALUES (?, ?, ?, ?, ?, ?, 'test', 0, '{}')
        """,
        (
            symbol,
            candle_hour,  # detected_at ~= candle_hour for test purposes
            candle_hour,
            price,
            score,
            severity,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_candle(
    conn,
    *,
    symbol: str = "BTCUSDT",
    open_time: datetime,
    open_: float,
    high: float,
    low: float,
    close: float,
    interval: str = "1h",
) -> None:
    open_iso = open_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    close_iso = (open_time + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO candles
            (symbol, interval, open_time, open, high, low, close, volume, close_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, 100.0, ?)
        """,
        (symbol, interval, open_iso, open_, high, low, close, close_iso),
    )


def _seed_future_candles(
    conn,
    *,
    symbol: str,
    anchor: datetime,
    price_at_signal: float,
    price_24h: float,
    price_7d: float,
    price_30d: float,
    window_7d_high: float,
    window_7d_low: float,
) -> None:
    """Insert exactly the candles the evaluator will look up.

    The evaluator uses 1h candles for all price lookups (24h / 7d /
    30d) AND for the 7-day high/low window. This helper places one
    candle at each lookup timestamp plus a high-water and low-water
    candle somewhere inside the 7d window, so we can assert the
    computed values precisely.
    """
    # The anchor candle itself. low < high so we have a valid row.
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor,
        open_=price_at_signal, high=price_at_signal,
        low=price_at_signal, close=price_at_signal,
    )

    # 24h later: exactly one 1h candle.
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor + timedelta(hours=24),
        open_=price_24h, high=price_24h, low=price_24h, close=price_24h,
    )

    # The 7-day high, placed mid-window (+3d).
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor + timedelta(days=3),
        open_=window_7d_high, high=window_7d_high,
        low=window_7d_high, close=window_7d_high,
    )

    # The 7-day low, placed later in the window (+4d).
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor + timedelta(days=4),
        open_=window_7d_low, high=window_7d_low,
        low=window_7d_low, close=window_7d_low,
    )

    # 7 days later.
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor + timedelta(days=7),
        open_=price_7d, high=price_7d, low=price_7d, close=price_7d,
    )

    # 30 days later.
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor + timedelta(days=30),
        open_=price_30d, high=price_30d, low=price_30d, close=price_30d,
    )
    conn.commit()


# ---------- matured signal ----------

def test_matured_signal_evaluation_end_to_end(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db,
        candle_hour=anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        price=40.0,
    )
    _seed_future_candles(
        memory_db,
        symbol="BTCUSDT",
        anchor=anchor,
        price_at_signal=40.0,
        price_24h=41.0,         # +2.5%
        price_7d=44.0,          # +10% → great
        price_30d=48.0,         # +20%
        window_7d_high=50.0,    # max_gain = +25%
        window_7d_low=38.0,     # max_loss = -5%
    )

    # Now is 31 days after anchor → fully matured.
    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )

    assert result is not None
    assert result.signal_id == signal_id
    assert result.price_at_signal == 40.0
    assert result.price_24h_later == 41.0
    assert result.price_7d_later == 44.0
    assert result.price_30d_later == 48.0
    assert result.return_24h_pct == pytest.approx(2.5)
    assert result.return_7d_pct == pytest.approx(10.0)
    assert result.return_30d_pct == pytest.approx(20.0)
    assert result.max_gain_7d_pct == pytest.approx(25.0)
    assert result.max_loss_7d_pct == pytest.approx(-5.0)
    # 10% exactly hits the great threshold.
    assert result.verdict == VERDICT_GREAT

    # Row persisted.
    row = memory_db.execute(
        "SELECT * FROM signal_evaluations WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert row is not None
    assert row["verdict"] == VERDICT_GREAT
    assert row["return_7d_pct"] == pytest.approx(10.0)


def test_good_verdict_from_moderate_gain(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, candle_hour="2026-03-01T14:00:00Z", price=100.0)
    _seed_future_candles(
        memory_db,
        symbol="BTCUSDT",
        anchor=anchor,
        price_at_signal=100.0,
        price_24h=102.0,
        price_7d=107.0,       # +7% → good
        price_30d=110.0,
        window_7d_high=108.0,
        window_7d_low=95.0,
    )
    now = anchor + timedelta(days=31)
    result = evaluate_signal(memory_db, signal_id, eval_settings=eval_settings, now=now)
    assert result is not None
    assert result.return_7d_pct == pytest.approx(7.0)
    assert result.verdict == VERDICT_GOOD


# ---------- not enough data ----------

def test_signal_too_young_is_pending(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, candle_hour="2026-03-01T14:00:00Z")

    # Only 10 days have passed since anchor — nowhere near 30d maturation.
    now = anchor + timedelta(days=10)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )

    assert result is None
    # No row should have been inserted.
    row = memory_db.execute(
        "SELECT 1 FROM signal_evaluations WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert row is None


def test_evaluate_pending_signals_skips_young_and_evaluates_mature(
    memory_db, eval_settings
):
    mature_anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    mature_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", symbol="BTCUSDT"
    )
    _seed_future_candles(
        memory_db,
        symbol="BTCUSDT",
        anchor=mature_anchor,
        price_at_signal=40.0,
        price_24h=41.0,
        price_7d=44.0,
        price_30d=48.0,
        window_7d_high=50.0,
        window_7d_low=38.0,
    )

    young_id = _insert_signal(
        memory_db,
        symbol="ETHUSDT",
        candle_hour="2026-03-25T14:00:00Z",
    )

    now = mature_anchor + timedelta(days=31)  # mature one is evaluatable, young one is not
    report = evaluate_pending_signals(
        memory_db, eval_settings=eval_settings, now=now
    )

    assert report.considered == 2
    assert report.evaluated == 1
    assert report.skipped_pending == 1

    # Only the mature signal has a row.
    mature_row = memory_db.execute(
        "SELECT 1 FROM signal_evaluations WHERE signal_id = ?",
        (mature_id,),
    ).fetchone()
    young_row = memory_db.execute(
        "SELECT 1 FROM signal_evaluations WHERE signal_id = ?",
        (young_id,),
    ).fetchone()
    assert mature_row is not None
    assert young_row is None


def test_missing_future_candles_yield_none_and_pending_verdict(
    memory_db, eval_settings
):
    # Signal is matured by wall clock, but the DB has NO future
    # candles — every price lookup misses → all returns None,
    # verdict = pending.
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, candle_hour="2026-03-01T14:00:00Z")
    now = anchor + timedelta(days=31)

    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.price_24h_later is None
    assert result.price_7d_later is None
    assert result.price_30d_later is None
    assert result.return_7d_pct is None
    assert result.max_gain_7d_pct is None
    assert result.max_loss_7d_pct is None
    # Block 24: timing fields stay None when the window has no candles.
    assert result.time_to_mfe_hours is None
    assert result.time_to_mae_hours is None
    assert result.verdict == VERDICT_PENDING


# ---------- rerun idempotency ----------

def test_rerunning_evaluation_is_a_noop(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, candle_hour="2026-03-01T14:00:00Z", price=40.0)
    _seed_future_candles(
        memory_db,
        symbol="BTCUSDT",
        anchor=anchor,
        price_at_signal=40.0,
        price_24h=41.0,
        price_7d=44.0,
        price_30d=48.0,
        window_7d_high=50.0,
        window_7d_low=38.0,
    )
    now = anchor + timedelta(days=31)

    first = evaluate_signal(memory_db, signal_id, eval_settings=eval_settings, now=now)
    second = evaluate_signal(memory_db, signal_id, eval_settings=eval_settings, now=now)

    assert first is not None
    assert second is None  # already evaluated → skipped
    assert memory_db.execute(
        "SELECT COUNT(*) FROM signal_evaluations WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()[0] == 1


# ---------- Block 24: MFE/MAE timing ----------

def test_mfe_and_mae_timing_match_seeded_window(memory_db, eval_settings):
    """Signal eval pins the bar that produced the MFE / MAE.

    The standard seed places the high mid-window (+3d → 72h) and the
    low later (+4d → 96h). Block 24 must surface those offsets on the
    result and persist them on the row.
    """
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", price=40.0
    )
    _seed_future_candles(
        memory_db,
        symbol="BTCUSDT",
        anchor=anchor,
        price_at_signal=40.0,
        price_24h=41.0,
        price_7d=44.0,
        price_30d=48.0,
        window_7d_high=50.0,
        window_7d_low=38.0,
    )

    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )

    assert result is not None
    assert result.max_gain_7d_pct == pytest.approx(25.0)
    assert result.max_loss_7d_pct == pytest.approx(-5.0)
    # +3d high → 72 hours; +4d low → 96 hours.
    assert result.time_to_mfe_hours == pytest.approx(72.0)
    assert result.time_to_mae_hours == pytest.approx(96.0)

    row = memory_db.execute(
        "SELECT time_to_mfe_hours, time_to_mae_hours "
        "FROM signal_evaluations WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert row["time_to_mfe_hours"] == pytest.approx(72.0)
    assert row["time_to_mae_hours"] == pytest.approx(96.0)


def test_timing_uses_earliest_bar_on_tie(memory_db, eval_settings):
    """When two bars share the extreme value the EARLIEST one wins."""
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", price=100.0
    )
    # Place the same high at +24h and +96h. The implementation must
    # return 24h for time_to_mfe_hours.
    _insert_candle(
        memory_db, symbol="BTCUSDT",
        open_time=anchor,
        open_=100.0, high=100.0, low=100.0, close=100.0,
    )
    _insert_candle(
        memory_db, symbol="BTCUSDT",
        open_time=anchor + timedelta(hours=24),
        open_=120.0, high=120.0, low=120.0, close=120.0,
    )
    _insert_candle(
        memory_db, symbol="BTCUSDT",
        open_time=anchor + timedelta(hours=96),
        open_=120.0, high=120.0, low=120.0, close=120.0,
    )
    # Need a 7d-later anchor so return_7d_pct can be computed (the
    # verdict path doesn't drive this test, but the row insert needs
    # *some* candle past the 7d window to mirror real usage).
    _insert_candle(
        memory_db, symbol="BTCUSDT",
        open_time=anchor + timedelta(days=7),
        open_=120.0, high=120.0, low=120.0, close=120.0,
    )
    memory_db.commit()

    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.time_to_mfe_hours == pytest.approx(24.0)


def test_window_with_only_anchor_candle_zero_offsets(memory_db, eval_settings):
    """A single bar at the anchor produces 0-hour offsets, not None."""
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", price=100.0
    )
    _insert_candle(
        memory_db, symbol="BTCUSDT",
        open_time=anchor,
        open_=100.0, high=110.0, low=90.0, close=100.0,
    )
    memory_db.commit()

    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.time_to_mfe_hours == pytest.approx(0.0)
    assert result.time_to_mae_hours == pytest.approx(0.0)
