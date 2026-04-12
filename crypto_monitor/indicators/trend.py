"""Exponential moving average and a simple trend label.

`ema(values, period)` returns the EMA series starting from the first
fully-formed value (after `period` warm-up candles). Earlier indices are
omitted to avoid pretending we have a value during warm-up.

`trend_label(closes, fast, slow)` returns one of:
  - 'uptrend'   : fast EMA is more than 1% above slow EMA
  - 'downtrend' : fast EMA is more than 1% below slow EMA
  - 'sideways'  : within ±1% (or insufficient history)

The 1% deadband prevents flapping when the two EMAs are nearly equal.
"""

from __future__ import annotations


_DEADBAND_PCT = 1.0


def ema(values: list[float], period: int) -> list[float]:
    """Return the EMA series. Empty list if `values` is shorter than `period`."""
    if period < 1 or len(values) < period:
        return []

    k = 2.0 / (period + 1)
    sma = sum(values[:period]) / period
    out: list[float] = [sma]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def trend_label(
    closes: list[float],
    fast_period: int = 20,
    slow_period: int = 50,
) -> str:
    """Return 'uptrend', 'sideways', or 'downtrend' based on the EMA cross.

    Defaults to 'sideways' when there isn't enough history to compute the
    slow EMA — neutral is the safe assumption when we can't tell.
    """
    if len(closes) < slow_period:
        return "sideways"

    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    if not fast or not slow:
        return "sideways"

    fast_last = fast[-1]
    slow_last = slow[-1]
    if slow_last == 0:
        return "sideways"

    diff_pct = (fast_last - slow_last) / slow_last * 100.0
    if diff_pct > _DEADBAND_PCT:
        return "uptrend"
    if diff_pct < -_DEADBAND_PCT:
        return "downtrend"
    return "sideways"
