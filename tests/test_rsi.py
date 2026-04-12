"""Tests for `crypto_monitor.indicators.rsi`.

Covers the four explicit cases requested for Block 5:
  - flat series         -> 50.0   (no movement, neutral)
  - monotonic up        -> ~100   (only gains, fully overbought)
  - monotonic down      -> ~0     (only losses, fully oversold)
  - insufficient history -> None
plus a couple of supporting edge cases.
"""

from __future__ import annotations

from crypto_monitor.indicators.rsi import rsi


# ---------- the four explicit cases the user listed ----------

def test_rsi_flat_series_returns_50():
    # Completely flat: avg_gain == avg_loss == 0. Must NOT pretend the
    # market is overbought — neutral 50 is the correct read.
    closes = [100.0] * 30
    assert rsi(closes, period=14) == 50.0


def test_rsi_monotonic_up_is_near_100():
    # Strictly increasing: every delta is a gain, no losses at all.
    closes = [float(100 + i) for i in range(30)]
    value = rsi(closes, period=14)
    assert value is not None
    assert value == 100.0


def test_rsi_monotonic_down_is_near_zero():
    # Strictly decreasing: every delta is a loss, no gains at all.
    closes = [float(200 - i) for i in range(30)]
    value = rsi(closes, period=14)
    assert value is not None
    assert value == 0.0


def test_rsi_insufficient_history_returns_none():
    # Need at least period + 1 data points.
    closes = [float(100 + i) for i in range(14)]  # only 14 -> 13 deltas
    assert rsi(closes, period=14) is None


# ---------- supporting cases ----------

def test_rsi_exactly_period_plus_one_is_enough():
    closes = [float(100 + i) for i in range(15)]  # 15 points -> 14 deltas
    value = rsi(closes, period=14)
    assert value is not None
    assert value == 100.0


def test_rsi_oscillating_balanced_series_is_around_50():
    # Alternating +1 / -1 moves: gains and losses average to the same
    # magnitude, so RSI should land near 50.
    closes: list[float] = [100.0]
    for i in range(30):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    value = rsi(closes, period=14)
    assert value is not None
    assert 40.0 <= value <= 60.0


def test_rsi_period_too_small_returns_none():
    closes = [float(i) for i in range(30)]
    assert rsi(closes, period=1) is None


def test_rsi_empty_series_returns_none():
    assert rsi([], period=14) is None


def test_rsi_value_in_range_for_mixed_series():
    closes = [
        100.0, 102.0, 101.5, 103.0, 102.0, 104.0, 103.5, 105.0,
        104.0, 106.0, 105.5, 107.0, 106.5, 108.0, 107.5, 109.0,
        108.5, 110.0, 109.5, 111.0,
    ]
    value = rsi(closes, period=14)
    assert value is not None
    assert 0.0 <= value <= 100.0
