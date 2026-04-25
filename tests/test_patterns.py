"""Tests for `crypto_monitor.indicators.patterns`."""

from __future__ import annotations

from crypto_monitor.indicators import Candle
from crypto_monitor.indicators.patterns import (
    detect_high_reclaim,
    detect_reversal,
    detect_rsi_recovery,
    is_bullish_engulfing,
    is_doji,
    is_hammer,
)


# ---------- is_hammer ----------

def test_hammer_red_body_long_lower_wick(make_candle):
    # Open 100, close 99 (small red body), low 92 (long lower wick), high 100.
    c = make_candle("t", 100.0, 100.0, 92.0, 99.0)
    assert is_hammer(c) is True


def test_hammer_green_body_long_lower_wick(make_candle):
    # Open 99, close 100 (small green body), low 92, high 100.
    c = make_candle("t", 99.0, 100.0, 92.0, 100.0)
    assert is_hammer(c) is True


def test_hammer_rejects_when_upper_wick_is_too_large(make_candle):
    # Long lower wick AND long upper wick -> not a hammer (more like a spinning top).
    c = make_candle("t", 100.0, 110.0, 92.0, 99.0)
    assert is_hammer(c) is False


def test_hammer_rejects_when_lower_wick_too_short_relative_to_body(make_candle):
    # Lower wick = 1 unit, body = 2 units -> 1 < 2*2 -> not a hammer.
    c = make_candle("t", 100.0, 102.5, 99.0, 102.0)
    assert is_hammer(c) is False


def test_hammer_rejects_doji_zero_body(make_candle):
    c = make_candle("t", 100.0, 100.5, 92.0, 100.0)
    assert is_hammer(c) is False  # body is zero


def test_hammer_rejects_zero_range(make_candle):
    c = make_candle("t", 100.0, 100.0, 100.0, 100.0)
    assert is_hammer(c) is False


# ---------- is_doji ----------

def test_doji_open_equals_close(make_candle):
    c = make_candle("t", 100.0, 102.0, 98.0, 100.0)
    assert is_doji(c) is True


def test_doji_tiny_body_relative_to_range(make_candle):
    # Body = 0.1, range = 4 -> 0.1 / 4 = 2.5% <= 10% -> doji.
    c = make_candle("t", 100.0, 102.0, 98.0, 100.1)
    assert is_doji(c) is True


def test_not_doji_when_body_too_large(make_candle):
    # Body = 1.0, range = 4 -> 25% > 10% -> not a doji.
    c = make_candle("t", 100.0, 102.0, 98.0, 101.0)
    assert is_doji(c) is False


def test_doji_rejects_zero_range(make_candle):
    c = make_candle("t", 100.0, 100.0, 100.0, 100.0)
    assert is_doji(c) is False


# ---------- is_bullish_engulfing ----------

def test_bullish_engulfing_basic(make_candle):
    prev = make_candle("t1", 100.0, 100.5, 97.0, 98.0)   # red: open 100 -> close 98
    curr = make_candle("t2", 97.5, 101.0, 97.0, 100.5)   # green and engulfs prev body
    assert is_bullish_engulfing(prev, curr) is True


def test_bullish_engulfing_requires_prev_red(make_candle):
    prev = make_candle("t1", 98.0, 100.5, 97.0, 100.0)   # green prev
    curr = make_candle("t2", 97.5, 101.0, 97.0, 100.5)   # green
    assert is_bullish_engulfing(prev, curr) is False


def test_bullish_engulfing_requires_curr_green(make_candle):
    prev = make_candle("t1", 100.0, 100.5, 97.0, 98.0)   # red
    curr = make_candle("t2", 100.0, 100.5, 96.0, 97.0)   # red
    assert is_bullish_engulfing(prev, curr) is False


def test_bullish_engulfing_requires_curr_body_to_engulf_prev(make_candle):
    prev = make_candle("t1", 100.0, 100.5, 97.0, 98.0)   # red body 100..98
    curr = make_candle("t2", 98.5, 99.5, 98.0, 99.0)     # green body 98.5..99.0 (does not engulf)
    assert is_bullish_engulfing(prev, curr) is False


# ---------- detect_reversal priority ----------

def test_detect_reversal_returns_hammer_first(make_candle):
    # Last candle is a hammer AND the prev/last pair forms a bullish engulfing.
    # Hammer has higher priority -> hammer wins.
    prev = make_candle("t1", 100.0, 100.5, 97.0, 98.0)   # red prev
    last = make_candle("t2", 99.0, 100.0, 92.0, 99.8)    # hammer-shaped + green engulfing
    result = detect_reversal([prev, last])
    assert result.detected is True
    assert result.pattern_name == "hammer"


def test_detect_reversal_engulfing_when_no_hammer(make_candle):
    prev = make_candle("t1", 100.0, 100.5, 97.0, 98.0)   # red
    last = make_candle("t2", 97.5, 101.0, 97.0, 100.5)   # green engulfing, NOT a hammer
    result = detect_reversal([prev, last])
    assert result.detected is True
    assert result.pattern_name == "bullish_engulfing"


def test_detect_reversal_doji_when_nothing_else(make_candle):
    last = make_candle("t1", 100.0, 102.0, 98.0, 100.0)
    result = detect_reversal([last])
    assert result.detected is True
    assert result.pattern_name == "doji"


def test_detect_reversal_none_when_neutral_candle(make_candle):
    # Plain green candle: not a hammer, not a doji, no prev for engulfing.
    last = make_candle("t1", 100.0, 102.0, 99.5, 101.5)
    result = detect_reversal([last])
    assert result.detected is False
    assert result.pattern_name is None


def test_detect_reversal_empty_list_returns_no_pattern():
    result = detect_reversal([])
    assert result.detected is False
    assert result.pattern_name is None


# ---------- detect_rsi_recovery ----------

def _crash_then_recover_closes() -> list[float]:
    """Build a closes series whose RSI dips into oversold then recovers.

    20 flat candles at 100 (RSI seed = 50ish), then a sharp 8-bar
    descent that pushes RSI well below 30, then a strong rebound that
    lifts the latest RSI back above 30.
    """
    closes = [100.0] * 20
    # Sharp descent — eight steps down by 5 each.
    closes += [100.0 - i * 5 for i in range(1, 9)]
    # Recovery: three strong green bars.
    closes += [65.0, 75.0, 85.0]
    return closes


def test_rsi_recovery_fires_after_dip_then_rebound():
    closes = _crash_then_recover_closes()
    assert detect_rsi_recovery(closes, period=14, lookback=5) is True


def test_rsi_recovery_false_when_still_oversold():
    # Pure descent — RSI stays oversold, no recovery.
    closes = [100.0 - i for i in range(40)]
    assert detect_rsi_recovery(closes, period=14, lookback=5) is False


def test_rsi_recovery_false_when_no_recent_dip():
    # Flat-then-up — RSI never enters oversold, so nothing to recover from.
    closes = [100.0] * 20 + [100.0 + i * 0.5 for i in range(1, 15)]
    assert detect_rsi_recovery(closes, period=14, lookback=5) is False


def test_rsi_recovery_false_with_insufficient_history():
    closes = [100.0, 99.0, 98.0]  # way under period + lookback
    assert detect_rsi_recovery(closes, period=14, lookback=5) is False


# ---------- detect_high_reclaim ----------

def _candle(idx: int, h: float, c: float) -> Candle:
    return Candle(
        open_time=f"t-{idx:04d}",
        open=c,
        high=h,
        low=min(c, h),
        close=c,
        volume=100.0,
        close_time=f"t-{idx:04d}c",
    )


def test_high_reclaim_fires_when_close_exceeds_prior_window_high():
    # 10 prior candles with highs around 100, then a final close at 105.
    candles = [_candle(i, 100.0, 99.0) for i in range(10)]
    candles.append(_candle(10, 105.0, 105.0))
    assert detect_high_reclaim(candles, lookback=10) is True


def test_high_reclaim_false_when_close_at_or_below_prior_high():
    candles = [_candle(i, 100.0, 99.0) for i in range(10)]
    # Close exactly equal to the prior window's max high — strict inequality required.
    candles.append(_candle(10, 100.0, 100.0))
    assert detect_high_reclaim(candles, lookback=10) is False


def test_high_reclaim_false_with_insufficient_history():
    candles = [_candle(i, 100.0, 99.0) for i in range(5)]
    assert detect_high_reclaim(candles, lookback=10) is False


def test_high_reclaim_excludes_latest_candle_from_comparison_window():
    # Latest candle's own high (110) is huge but it must reclaim only
    # the *prior* window's max (100).
    candles = [_candle(i, 100.0, 99.0) for i in range(10)]
    candles.append(_candle(10, 110.0, 105.0))
    assert detect_high_reclaim(candles, lookback=10) is True
