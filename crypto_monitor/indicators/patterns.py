"""Reversal candlestick pattern detection + confirmation helpers.

Three candlestick patterns, in priority order (highest priority first
because `detect_reversal` returns at most one):

  1. Hammer        — small body, long lower wick (>=2x body), tiny upper wick.
                     Strongest local-bottom signal.
  2. Bullish engulfing — previous candle red, current candle green, current
                         body fully engulfs previous body.
  3. Doji          — open ~= close (body <= 10% of total range). Indecision;
                     weak on its own but useful as confirmation.

Two confirmation helpers added in Block 17:

  * `detect_rsi_recovery`   — RSI dipped into oversold in the recent past
                              and is back above the oversold threshold.
  * `detect_high_reclaim`   — latest close exceeds the highest HIGH of the
                              previous N bars, i.e. reclaims prior resistance.

All candlestick checks are pure ratio rules over a candle's own OHLC.
The confirmation helpers operate over short windows and never reach
back further than their `lookback` parameter.
"""

from __future__ import annotations

from crypto_monitor.indicators.rsi import rsi
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


# ---------- confirmation helpers (Block 17) ----------

def detect_rsi_recovery(
    closes: list[float],
    *,
    period: int = 14,
    oversold: float = 30.0,
    lookback: int = 5,
) -> bool:
    """Return True when RSI recently dipped into oversold and has recovered.

    Concretely: the current RSI is strictly above ``oversold`` AND at
    least one of the ``lookback - 1`` preceding bars had an RSI value
    at or below ``oversold``. The idea is a momentum turnaround: price
    made a capitulation low (RSI ≤ 30) and buyers have now pushed RSI
    back above the threshold.

    Returns False when:
      * there is not enough history to compute RSI for every lookback bar
      * current RSI is itself still in oversold (no recovery)
      * no recent bar was oversold (no dip to recover from)
    """
    if lookback < 2:
        return False
    if len(closes) < period + lookback:
        return False

    rsi_now = rsi(closes, period=period)
    if rsi_now is None or rsi_now <= oversold:
        return False

    # Walk back `lookback - 1` bars; if any past RSI reading was oversold,
    # the current reading represents a recovery.
    for k in range(1, lookback):
        rsi_past = rsi(closes[:-k], period=period)
        if rsi_past is not None and rsi_past <= oversold:
            return True
    return False


def detect_high_reclaim(
    candles: list[Candle],
    *,
    lookback: int = 10,
) -> bool:
    """Return True when the latest close reclaims a prior local high.

    Specifically: the latest candle's ``close`` is strictly greater
    than the maximum ``high`` across the previous ``lookback`` candles
    (the latest candle itself is excluded from the comparison window).

    Returns False when there is not enough history (``len(candles) <
    lookback + 1``) or the latest close fails to exceed prior resistance.
    """
    if lookback < 1:
        return False
    if len(candles) < lookback + 1:
        return False
    latest_close = candles[-1].close
    window = candles[-(lookback + 1):-1]
    prior_high = max(c.high for c in window)
    return latest_close > prior_high
