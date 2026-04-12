"""Reversal candlestick pattern detection.

Three patterns, in priority order (highest priority first because
`detect_reversal` returns at most one):

  1. Hammer        — small body, long lower wick (>=2x body), tiny upper wick.
                     Strongest local-bottom signal.
  2. Bullish engulfing — previous candle red, current candle green, current
                         body fully engulfs previous body.
  3. Doji          — open ~= close (body <= 10% of total range). Indecision;
                     weak on its own but useful as confirmation.

All checks are pure ratio rules over the candle's own OHLC values, so
they're trivially testable and don't require any historical context
beyond the previous candle (for engulfing).
"""

from __future__ import annotations

from crypto_monitor.indicators.types import Candle, ReversalInfo


def _body(c: Candle) -> float:
    return abs(c.close - c.open)


def is_hammer(c: Candle) -> bool:
    body = _body(c)
    if body == 0:
        return False
    candle_range = c.high - c.low
    if candle_range <= 0:
        return False
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    # Lower wick must dominate the body, AND the upper wick must be small
    # relative to the total candle range. Comparing the upper wick to the
    # range (rather than to the body) keeps the rule robust when the body
    # itself is tiny — a thin-bodied long-lower-wick candle is the
    # archetypal hammer and shouldn't be rejected just because 0.5 * body
    # is a microscopic threshold.
    return lower_wick >= 2.0 * body and upper_wick <= 0.25 * candle_range


def is_doji(c: Candle) -> bool:
    candle_range = c.high - c.low
    if candle_range <= 0:
        return False
    return _body(c) / candle_range <= 0.10


def is_bullish_engulfing(prev: Candle, curr: Candle) -> bool:
    prev_red = prev.close < prev.open
    curr_green = curr.close > curr.open
    if not (prev_red and curr_green):
        return False
    return curr.open <= prev.close and curr.close >= prev.open


def detect_reversal(candles: list[Candle]) -> ReversalInfo:
    """Inspect the most recent (and previous, for engulfing) closed candle.

    Returns a `ReversalInfo` with `detected=False` and `pattern_name=None`
    when nothing is found. The signal engine treats `detected=True` as a
    positive factor with the `pattern_name` recorded for the score breakdown.
    """
    if not candles:
        return ReversalInfo(False, None)

    last = candles[-1]
    if is_hammer(last):
        return ReversalInfo(True, "hammer")

    if len(candles) >= 2 and is_bullish_engulfing(candles[-2], last):
        return ReversalInfo(True, "bullish_engulfing")

    if is_doji(last):
        return ReversalInfo(True, "doji")

    return ReversalInfo(False, None)
