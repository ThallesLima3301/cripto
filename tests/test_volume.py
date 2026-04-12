"""Tests for `crypto_monitor.indicators.volume`."""

from __future__ import annotations

from crypto_monitor.indicators.volume import relative_volume


def test_relative_volume_equal_to_baseline_is_one():
    volumes = [100.0] * 20 + [100.0]
    assert relative_volume(volumes, period=20) == 1.0


def test_relative_volume_double_baseline_is_two():
    volumes = [100.0] * 20 + [200.0]
    assert relative_volume(volumes, period=20) == 2.0


def test_relative_volume_half_baseline_is_half():
    volumes = [100.0] * 20 + [50.0]
    assert relative_volume(volumes, period=20) == 0.5


def test_relative_volume_uses_only_prior_window_not_recent_candle():
    # Baseline is the 20 candles BEFORE the latest one. The latest candle
    # itself must not skew the average.
    volumes = [10.0] * 20 + [1000.0]
    # Baseline = 10, recent = 1000 -> rel = 100
    assert relative_volume(volumes, period=20) == 100.0


def test_relative_volume_insufficient_history_returns_none():
    volumes = [100.0] * 20  # need period + 1
    assert relative_volume(volumes, period=20) is None


def test_relative_volume_zero_baseline_returns_none():
    # If every prior candle is zero we cannot compute a ratio.
    volumes = [0.0] * 20 + [100.0]
    assert relative_volume(volumes, period=20) is None


def test_relative_volume_period_smaller_than_default():
    volumes = [10.0, 10.0, 10.0, 10.0, 10.0, 30.0]
    # period=5, baseline=10, recent=30 -> 3.0
    assert relative_volume(volumes, period=5) == 3.0


def test_relative_volume_empty_series_returns_none():
    assert relative_volume([], period=20) is None
