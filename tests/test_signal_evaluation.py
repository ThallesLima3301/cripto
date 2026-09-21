"""Tests for `crypto_monitor.evaluation.signal_eval`.

Covers:
  * matured signal evaluation end-to-end (24h/7d/30d returns,
    max_gain/max_loss over the 7d window, verdict assignment, row
    inserted into signal_evaluations)
  * pending / not-enough-future-data behavior (signal too young →
    no row written, reported as skipped_pending)
  * retrying incomplete evaluations without duplicating rows or losing data
  * closed-candle timing, complete excursion windows, and bounded lookups
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
    detected_at: str | None = None,
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
            detected_at or candle_hour,
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
    """Seed the complete 7-day window after the signal candle closes.

    ``anchor`` is the opening of the signal candle. Availability is
    one hour later, so the 24h/7d/30d target prices are the closes of
    candles opening at anchor +24h/+7d/+30d respectively.
    """
    _insert_candle(
        conn, symbol=symbol,
        open_time=anchor,
        open_=price_at_signal, high=price_at_signal,
        low=price_at_signal, close=price_at_signal,
    )

    special_prices = {
        24: price_24h,
        72: window_7d_high,
        96: window_7d_low,
        168: price_7d,
    }
    for hours in range(1, 169):
        price = special_prices.get(hours, price_at_signal)
        _insert_candle(
            conn, symbol=symbol,
            open_time=anchor + timedelta(hours=hours),
            open_=price, high=price, low=price, close=price,
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

    The high and low are 71h and 95h after signal availability,
    measured at their candle openings.
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
    assert result.time_to_mfe_hours == pytest.approx(71.0)
    assert result.time_to_mae_hours == pytest.approx(95.0)

    row = memory_db.execute(
        "SELECT time_to_mfe_hours, time_to_mae_hours "
        "FROM signal_evaluations WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    assert row["time_to_mfe_hours"] == pytest.approx(71.0)
    assert row["time_to_mae_hours"] == pytest.approx(95.0)


def test_timing_uses_earliest_bar_on_tie(memory_db, eval_settings):
    """When two bars share the extreme value the EARLIEST one wins."""
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", price=100.0
    )
    # Equal highs at +24h and +96h are 23h and 95h after availability.
    for hours in range(1, 169):
        price = 120.0 if hours in (24, 96) else 100.0
        _insert_candle(
            memory_db, open_time=anchor + timedelta(hours=hours),
            open_=price, high=price, low=price, close=price,
        )
    memory_db.commit()

    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.time_to_mfe_hours == pytest.approx(23.0)


def test_signal_candle_extremes_are_excluded(memory_db, eval_settings):
    """A +150%/-80% move before the signal existed is not performance."""
    anchor = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    signal_id = _insert_signal(
        memory_db, candle_hour="2026-03-01T14:00:00Z", price=100.0
    )
    _seed_future_candles(
        memory_db, symbol="BTCUSDT", anchor=anchor,
        price_at_signal=100.0, price_24h=100.0,
        price_7d=100.0, price_30d=100.0,
        window_7d_high=105.0, window_7d_low=95.0,
    )
    memory_db.execute(
        "UPDATE candles SET high = 250.0, low = 20.0 WHERE open_time = ?",
        (anchor.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    memory_db.commit()

    now = anchor + timedelta(days=31)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    )
    assert result is not None
    assert result.max_gain_7d_pct == pytest.approx(5.0)
    assert result.max_loss_7d_pct == pytest.approx(-5.0)
    assert result.time_to_mfe_hours == pytest.approx(71.0)
    assert result.time_to_mae_hours == pytest.approx(95.0)


@pytest.mark.parametrize("elapsed_hours", [720, 720 + 59 / 60, 721])
def test_maturation_starts_when_signal_candle_closes(
    memory_db, eval_settings, elapsed_hours
):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    signal_id = _insert_signal(memory_db)
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=anchor + timedelta(hours=elapsed_hours),
    )
    assert (result is not None) == (elapsed_hours == 721)
    count = memory_db.execute("SELECT COUNT(*) FROM signal_evaluations").fetchone()[0]
    assert count == int(elapsed_hours == 721)


def test_delayed_detection_moves_maturation_and_all_horizons(
    memory_db, eval_settings
):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    available = anchor + timedelta(hours=2, minutes=5)
    signal_id = _insert_signal(
        memory_db, price=100.0,
        detected_at=available.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    # Only 167 complete candles fit between 16:05 and 16:05 seven days later.
    for hours in range(3, 170):
        price = 110.0 if hours == 26 else 100.0
        _insert_candle(
            memory_db, open_time=anchor + timedelta(hours=hours),
            open_=price, high=price, low=price, close=price,
        )
    # Partial candles on both edges must not contribute excursions.
    for hours, price in ((2, 100.0), (170, 120.0), (722, 130.0)):
        _insert_candle(
            memory_db, open_time=anchor + timedelta(hours=hours),
            open_=100.0, high=300.0, low=10.0, close=price,
        )
    memory_db.commit()

    assert evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=available + timedelta(days=30) - timedelta(seconds=1),
    ) is None
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=available + timedelta(days=30),
    )
    assert result is not None
    assert result.price_24h_later == 110.0
    assert result.price_7d_later == 120.0
    # The 30d candle is present but still open at 16:05.
    assert result.price_30d_later is None
    assert result.max_gain_7d_pct == pytest.approx(10.0)
    assert result.max_loss_7d_pct == pytest.approx(0.0)
    assert result.time_to_mfe_hours == pytest.approx(23 + 55 / 60)

    completed = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=anchor + timedelta(days=30, hours=3),
    )
    assert completed is not None
    assert completed.price_30d_later == 130.0


def test_missing_24h_candle_does_not_substitute_day_7(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, price=100.0)
    _insert_candle(
        memory_db, open_time=anchor + timedelta(days=7),
        open_=150.0, high=150.0, low=150.0, close=150.0,
    )
    memory_db.commit()
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=anchor + timedelta(days=31),
    )
    assert result is not None
    assert result.price_24h_later is None
    assert result.return_24h_pct is None
    assert result.price_7d_later == 150.0
    assert result.max_gain_7d_pct is None
    assert result.max_loss_7d_pct is None


def test_missing_window_candle_is_retried_and_completes_same_row(
    memory_db, eval_settings
):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, price=100.0)
    _seed_future_candles(
        memory_db, symbol="BTCUSDT", anchor=anchor,
        price_at_signal=100.0, price_24h=101.0,
        price_7d=110.0, price_30d=120.0,
        window_7d_high=125.0, window_7d_low=90.0,
    )
    gap = anchor + timedelta(hours=50)
    memory_db.execute(
        "DELETE FROM candles WHERE open_time = ?",
        (gap.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    memory_db.commit()
    now = anchor + timedelta(days=31)
    first = evaluate_signal(memory_db, signal_id, eval_settings=eval_settings, now=now)
    assert first is not None
    assert first.return_7d_pct == pytest.approx(10.0)
    assert first.max_gain_7d_pct is None
    assert first.max_loss_7d_pct is None
    row_before = memory_db.execute(
        "SELECT * FROM signal_evaluations WHERE signal_id = ?", (signal_id,)
    ).fetchone()
    assert row_before["max_gain_7d_pct"] is None
    assert row_before["time_to_mfe_hours"] is None

    _insert_candle(
        memory_db, open_time=gap,
        open_=100.0, high=100.0, low=100.0, close=100.0,
    )
    memory_db.commit()
    report = evaluate_pending_signals(
        memory_db, eval_settings=eval_settings, now=now + timedelta(hours=1)
    )
    assert report.considered == 1
    assert report.evaluated == 1
    assert report.skipped_pending == 0
    row_after = memory_db.execute(
        "SELECT * FROM signal_evaluations WHERE signal_id = ?", (signal_id,)
    ).fetchone()
    assert row_after["id"] == row_before["id"]
    assert row_after["max_gain_7d_pct"] == pytest.approx(25.0)
    assert row_after["max_loss_7d_pct"] == pytest.approx(-10.0)
    assert row_after["evaluated_at"] != row_before["evaluated_at"]
    assert memory_db.execute("SELECT COUNT(*) FROM signal_evaluations").fetchone()[0] == 1

    final = evaluate_pending_signals(
        memory_db, eval_settings=eval_settings, now=now + timedelta(hours=2)
    )
    assert final.considered == 0
    assert final.evaluated == 0


def test_unchanged_partial_retry_preserves_timestamp(memory_db, eval_settings):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    signal_id = _insert_signal(memory_db)
    now = anchor + timedelta(days=31)
    assert evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings, now=now
    ) is not None
    before = dict(memory_db.execute("SELECT * FROM signal_evaluations").fetchone())
    assert evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=now + timedelta(hours=1),
    ) is None
    report = evaluate_pending_signals(
        memory_db, eval_settings=eval_settings, now=now + timedelta(hours=2)
    )
    assert report.considered == 1
    assert report.evaluated == 0
    assert report.skipped_pending == 0
    assert dict(memory_db.execute("SELECT * FROM signal_evaluations").fetchone()) == before


def test_partial_retry_preserves_results_after_candles_are_pruned(
    memory_db, eval_settings
):
    anchor = datetime(2026, 3, 1, 14, tzinfo=UTC)
    signal_id = _insert_signal(memory_db, price=100.0)
    _seed_future_candles(
        memory_db, symbol="BTCUSDT", anchor=anchor,
        price_at_signal=100.0, price_24h=101.0,
        price_7d=110.0, price_30d=120.0,
        window_7d_high=125.0, window_7d_low=90.0,
    )
    horizon_30d = anchor + timedelta(days=30)
    memory_db.execute(
        "DELETE FROM candles WHERE open_time = ?",
        (horizon_30d.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    memory_db.commit()
    now = anchor + timedelta(days=31)
    first = evaluate_signal(memory_db, signal_id, eval_settings=eval_settings, now=now)
    assert first is not None
    assert first.price_30d_later is None
    assert first.max_gain_7d_pct == pytest.approx(25.0)
    memory_db.execute("DELETE FROM candles")
    _insert_candle(
        memory_db, open_time=horizon_30d,
        open_=120.0, high=120.0, low=120.0, close=120.0,
    )
    memory_db.commit()
    result = evaluate_signal(
        memory_db, signal_id, eval_settings=eval_settings,
        now=now + timedelta(hours=1),
    )
    assert result is not None
    assert result.price_24h_later == 101.0
    assert result.price_7d_later == 110.0
    assert result.price_30d_later == 120.0
    assert result.return_24h_pct == pytest.approx(1.0)
    assert result.return_7d_pct == pytest.approx(10.0)
    assert result.return_30d_pct == pytest.approx(20.0)
    assert result.max_gain_7d_pct == pytest.approx(25.0)
    assert result.max_loss_7d_pct == pytest.approx(-10.0)
    assert result.time_to_mfe_hours == first.time_to_mfe_hours
    assert result.time_to_mae_hours == first.time_to_mae_hours
    assert result.verdict == VERDICT_GREAT
    row = memory_db.execute("SELECT * FROM signal_evaluations").fetchone()
    assert row["return_24h_pct"] == pytest.approx(1.0)
    assert row["max_gain_7d_pct"] == pytest.approx(25.0)
    assert row["return_30d_pct"] == pytest.approx(20.0)
