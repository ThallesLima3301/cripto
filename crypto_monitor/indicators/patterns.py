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


def detect_bullish_divergence(
    candles: list[Candle],
    rsi_values: list[float | None],
    *,
    window: int = 14,
) -> bool:
    """Return True when price prints a lower low while RSI prints a higher low.

    Block 27, optional refinement. Conservative pivot logic:

      1. Look at the last ``window`` candles and the matching tail of
         ``rsi_values``. Both inputs must be at least ``window`` long
         and aligned by index.
      2. Split that tail into an older half and a newer half (the
         oldest item of the window is index 0).
      3. Find the bar with the lowest ``close`` in each half.
      4. Divergence fires when:
            - ``close[newer_low] < close[older_low]``  (price = lower low),
            - ``rsi[newer_low]   > rsi[older_low]``    (RSI   = higher low),
            both inequalities strict so flat / equal data does not
            register as a signal.

    Returns ``False`` on insufficient history (``len(candles) < window``
    or fewer aligned RSI values), ``window < 4`` (no two halves), or
    when either pivot's RSI value is ``None``.

    The detector is pure: no DB, no I/O. The caller (engine) computes
    the RSI series via ``indicators.rsi.rsi_series`` and slices the
    aligned tail.
    """
    if window < 4:
        return False
    n = len(candles)
    if n < window or len(rsi_values) < window:
        return False

    closes_tail = [c.close for c in candles[-window:]]
    rsi_tail = list(rsi_values[-window:])
    half = window // 2

    older_idx = _argmin_close(closes_tail[:half])
    newer_idx_local = _argmin_close(closes_tail[half:])
    if older_idx is None or newer_idx_local is None:
        return False
    newer_idx = half + newer_idx_local

    rsi_older = rsi_tail[older_idx]
    rsi_newer = rsi_tail[newer_idx]
    if rsi_older is None or rsi_newer is None:
        return False

    price_lower_low = closes_tail[newer_idx] < closes_tail[older_idx]
    rsi_higher_low = rsi_newer > rsi_older
    return price_lower_low and rsi_higher_low


def _argmin_close(values: list[float]) -> int | None:
    """Return the index of the lowest value (earliest-tie wins), or None."""
    if not values:
        return None
    best_i = 0
    best_v = values[0]
    for i in range(1, len(values)):
        if values[i] < best_v:
            best_v = values[i]
            best_i = i
    return best_i
