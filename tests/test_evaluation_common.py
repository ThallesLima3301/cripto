"""Temporal boundaries shared by signal and buy evaluations."""

from datetime import datetime, timedelta, timezone

import pytest

from crypto_monitor.evaluation.common import (
    max_gain_loss_with_timing,
    price_at_horizon,
)


UTC = timezone.utc


def _candle(conn, opened, *, high=105.0, low=95.0, close=100.0, symbol="BTCUSDT"):
    conn.execute(
        """
        INSERT INTO candles
            (symbol, interval, open_time, open, high, low, close, volume, close_time)
        VALUES (?, '1h', ?, ?, ?, ?, ?, 100, ?)
        """,
        (
            symbol, opened.strftime("%Y-%m-%dT%H:%M:%SZ"), close,
            high, low, close,
            (opened + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )


def test_exact_horizon_uses_candle_closing_at_target(memory_db):
    target = datetime(2026, 3, 2, 15, tzinfo=UTC)
    _candle(memory_db, target - timedelta(hours=1), close=101.0)
    _candle(memory_db, target, close=999.0)

    assert price_at_horizon(memory_db, "BTCUSDT", target, now=target) == 101.0


def test_fractional_horizon_waits_for_next_hourly_close(memory_db):
    opening = datetime(2026, 3, 2, 15, tzinfo=UTC)
    target = opening + timedelta(minutes=5)
    _candle(memory_db, opening - timedelta(hours=1), close=99.0)
    _candle(memory_db, opening, close=101.0)
    _candle(memory_db, opening + timedelta(hours=1), close=999.0)

    assert price_at_horizon(memory_db, "BTCUSDT", target, now=target) is None
    assert price_at_horizon(
        memory_db, "BTCUSDT", target,
        now=opening + timedelta(hours=1) - timedelta(seconds=1),
    ) is None
    assert price_at_horizon(
        memory_db, "BTCUSDT", target, now=opening + timedelta(hours=1),
    ) == 101.0


@pytest.mark.parametrize("delay", [timedelta(hours=1), timedelta(days=6)])
def test_missing_target_candle_cannot_be_replaced_by_later_data(memory_db, delay):
    target = datetime(2026, 3, 2, 15, tzinfo=UTC)
    _candle(memory_db, target - timedelta(hours=2), close=90.0)
    # A close exactly target +1h is already outside the allowed interval.
    _candle(memory_db, target + delay - timedelta(hours=1), close=150.0)
    _candle(memory_db, target - timedelta(hours=1), symbol="ETHUSDT", close=200.0)

    assert price_at_horizon(
        memory_db, "BTCUSDT", target, now=target + timedelta(days=30),
    ) is None


def test_horizon_never_reads_a_future_close(memory_db):
    target = datetime(2026, 3, 2, 15, tzinfo=UTC)
    _candle(memory_db, target - timedelta(hours=1), close=101.0)

    assert price_at_horizon(
        memory_db, "BTCUSDT", target, now=target - timedelta(seconds=1),
    ) is None


def test_excursion_excludes_bars_outside_complete_window(memory_db):
    start = datetime(2026, 3, 1, 15, tzinfo=UTC)
    end = start + timedelta(days=7)
    _candle(memory_db, start - timedelta(hours=1), high=250.0, low=20.0)
    _candle(memory_db, end, high=300.0, low=10.0)
    for hours in range(168):
        _candle(memory_db, start + timedelta(hours=hours))

    result = max_gain_loss_with_timing(
        memory_db, symbol="BTCUSDT", start=start, end=end, base=100.0,
    )
    assert result == pytest.approx((5.0, -5.0, 0.0, 0.0))


@pytest.mark.parametrize("missing_hour", [0, 83, 167])
def test_excursion_requires_every_hour_in_window(memory_db, missing_hour):
    start = datetime(2026, 3, 1, 15, tzinfo=UTC)
    for hours in range(168):
        if hours != missing_hour:
            _candle(memory_db, start + timedelta(hours=hours))

    result = max_gain_loss_with_timing(
        memory_db, symbol="BTCUSDT", start=start,
        end=start + timedelta(days=7), base=100.0,
    )
    assert result == (None, None, None, None)


def test_fractional_window_uses_only_167_complete_candles(memory_db):
    opening = datetime(2026, 3, 1, 15, tzinfo=UTC)
    start = opening + timedelta(minutes=5)
    _candle(memory_db, opening, high=250.0, low=20.0)
    _candle(memory_db, opening + timedelta(days=7), high=300.0, low=10.0)
    for hours in range(1, 168):
        _candle(memory_db, opening + timedelta(hours=hours))

    result = max_gain_loss_with_timing(
        memory_db, symbol="BTCUSDT", start=start,
        end=start + timedelta(days=7), base=100.0,
    )
    assert result == pytest.approx((5.0, -5.0, 55 / 60, 55 / 60))


@pytest.mark.parametrize(
    "high,low,expected_gain,expected_loss",
    [(90.0, 80.0, 0.0, -20.0), (120.0, 110.0, 20.0, 0.0)],
)
def test_excursions_are_clamped_to_the_entry_price(
    memory_db, high, low, expected_gain, expected_loss
):
    start = datetime(2026, 3, 1, 15, tzinfo=UTC)
    for hours in range(168):
        _candle(
            memory_db, start + timedelta(hours=hours),
            high=high, low=low, close=(high + low) / 2,
        )
    result = max_gain_loss_with_timing(
        memory_db, symbol="BTCUSDT", start=start,
        end=start + timedelta(days=7), base=100.0,
    )
    assert result[:2] == pytest.approx((expected_gain, expected_loss))


def test_excursion_ties_choose_first_occurrence_of_each_extreme(memory_db):
    start = datetime(2026, 3, 1, 15, tzinfo=UTC)
    for hours in range(168):
        _candle(
            memory_db, start + timedelta(hours=hours),
            high=120.0 if hours in (4, 9) else 105.0,
            low=80.0 if hours in (7, 15) else 95.0,
        )
    result = max_gain_loss_with_timing(
        memory_db, symbol="BTCUSDT", start=start,
        end=start + timedelta(days=7), base=100.0,
    )
    assert result == pytest.approx((20.0, -20.0, 4.0, 7.0))
