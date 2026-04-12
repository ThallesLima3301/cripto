"""Tests for `crypto_monitor.indicators.support`.

These tests build small synthetic daily candle series and check that
`find_heuristic_support` picks the closest swing-low at-or-below the
current price.
"""

from __future__ import annotations

from crypto_monitor.indicators import Candle
from crypto_monitor.indicators.support import find_heuristic_support


def _c(idx: int, low: float, high: float | None = None) -> Candle:
    """Build a daily candle. `idx` becomes the open_time string."""
    if high is None:
        high = low + 5.0
    return Candle(
        open_time=f"day-{idx}",
        open=low + 1.0,
        high=high,
        low=low,
        close=low + 2.0,
        volume=100.0,
        close_time=f"day-{idx}",
    )


def test_support_returns_none_when_history_too_short():
    candles = [_c(i, 100.0) for i in range(5)]  # need 2*3 + 1 = 7 with default
    assert find_heuristic_support(candles, current_price=100.0) is None


def test_support_returns_none_when_no_swing_low_below_price():
    # Strictly rising lows -> no swing low ever forms below current price.
    candles = [_c(i, 100.0 + i) for i in range(30)]
    result = find_heuristic_support(candles, current_price=80.0)
    assert result is None


def test_support_picks_obvious_v_bottom():
    # Build a clear V-shape: lows fall to a single bottom and rise back.
    lows = [110.0, 108.0, 106.0, 104.0, 102.0, 90.0, 102.0, 104.0, 106.0, 108.0, 110.0]
    candles = [_c(i, low) for i, low in enumerate(lows)]
    result = find_heuristic_support(candles, current_price=120.0)
    assert result is not None
    assert result.price == 90.0
    # current=120, support=90 -> distance = (120-90)/90 * 100 = 33.33...
    assert abs(result.distance_pct - ((120.0 - 90.0) / 90.0 * 100.0)) < 1e-9


def test_support_returns_closest_when_multiple_swing_lows():
    # Two distinct swing lows: a deep one (80) and a shallower one (95).
    # Both sit below current price (100) -> the shallower (closest) wins.
    lows = [
        110.0, 105.0, 100.0,
        80.0,                       # swing low #1 (deep)
        100.0, 105.0, 102.0,
        95.0,                       # swing low #2 (shallower, closer)
        102.0, 108.0, 110.0,
    ]
    candles = [_c(i, low) for i, low in enumerate(lows)]
    result = find_heuristic_support(candles, current_price=100.0)
    assert result is not None
    assert result.price == 95.0


def test_support_ignores_swing_lows_above_current_price():
    # The only swing low is above current price -> should be skipped.
    lows = [120.0, 118.0, 115.0, 110.0, 115.0, 118.0, 120.0]
    candles = [_c(i, low) for i, low in enumerate(lows)]
    result = find_heuristic_support(candles, current_price=100.0)
    assert result is None


def test_support_returns_none_for_zero_or_negative_price():
    candles = [_c(i, 100.0) for i in range(20)]
    assert find_heuristic_support(candles, current_price=0.0) is None
    assert find_heuristic_support(candles, current_price=-1.0) is None


def test_support_lookback_truncates_old_candles():
    # Older swing low (80) lies outside the lookback window;
    # within the window the only swing low is at 95.
    old_lows = [105.0, 100.0, 95.0, 80.0, 95.0, 100.0, 105.0]   # ancient deep V
    new_lows = [110.0, 108.0, 106.0, 95.0, 106.0, 108.0, 110.0]  # recent shallow V
    candles = [_c(i, low) for i, low in enumerate(old_lows + new_lows)]
    result = find_heuristic_support(
        candles,
        current_price=120.0,
        lookback_days=len(new_lows),
    )
    assert result is not None
    assert result.price == 95.0  # the recent one, not the ancient 80
