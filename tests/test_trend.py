"""Tests for `crypto_monitor.indicators.trend`."""

from __future__ import annotations

from crypto_monitor.indicators.trend import ema, trend_label


# ---------- ema ----------

def test_ema_returns_empty_when_too_short():
    assert ema([1.0, 2.0, 3.0], period=5) == []


def test_ema_first_value_is_sma_of_warmup():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    result = ema(values, period=5)
    # First value is the SMA of the first 5 inputs.
    assert result[0] == 3.0
    # Then one more value for the 6th input.
    assert len(result) == 2


def test_ema_constant_series_is_constant():
    values = [10.0] * 30
    result = ema(values, period=10)
    assert all(abs(v - 10.0) < 1e-9 for v in result)


def test_ema_period_one_is_just_input_series():
    values = [1.0, 2.0, 3.0, 4.0]
    result = ema(values, period=1)
    # period=1: SMA of first element is itself, then k = 2/2 = 1, so each
    # subsequent value collapses to the input value.
    assert result == values


def test_ema_invalid_period_returns_empty():
    assert ema([1.0, 2.0], period=0) == []


# ---------- trend_label ----------

def test_trend_label_uptrend_when_fast_well_above_slow():
    # Long, strongly rising series -> fast EMA > slow EMA by well over 1%.
    closes = [float(100 + i) for i in range(60)]
    assert trend_label(closes, fast_period=20, slow_period=50) == "uptrend"


def test_trend_label_downtrend_when_fast_well_below_slow():
    closes = [float(200 - i) for i in range(60)]
    assert trend_label(closes, fast_period=20, slow_period=50) == "downtrend"


def test_trend_label_sideways_when_flat():
    closes = [100.0] * 60
    assert trend_label(closes, fast_period=20, slow_period=50) == "sideways"


def test_trend_label_sideways_when_insufficient_history():
    closes = [float(100 + i) for i in range(30)]  # need >= slow_period (50)
    assert trend_label(closes, fast_period=20, slow_period=50) == "sideways"


def test_trend_label_sideways_within_deadband():
    # Tiny upward drift — fast vs slow should land within ±1%.
    closes = [100.0 + i * 0.001 for i in range(60)]
    assert trend_label(closes, fast_period=20, slow_period=50) == "sideways"
