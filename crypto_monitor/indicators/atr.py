"""Average True Range — Wilder's smoothing.

Computes the ATR for a candle series, consistent with the Wilder
smoothing used in `rsi.py`.

Definition (period = 14):
  TR[0]    = high[0] - low[0]                           (first candle)
  TR[i]    = max(high[i] - low[i],
                 |high[i] - close[i-1]|,
                 |low[i]  - close[i-1]|)                (i >= 1)

  ATR_seed = mean(TR[0..period-1])                      (SMA of first period TRs)
  ATR[i]   = (ATR[i-1] * (period - 1) + TR[i]) / period (Wilder smoothing)

Returns `None` if there are fewer than `period` candles.

Edge cases:
  - single candle    -> None (need at least `period` candles)
  - flat candles     -> ATR = 0.0 (no volatility)
  - gaps (open != prev close) are captured by the |high - prev_close|
    and |low - prev_close| terms in TR, matching the standard definition.
"""

from __future__ import annotations

from crypto_monitor.indicators.types import Candle


def true_range(candles: list[Candle]) -> list[float]:
    """Compute True Range for each candle.

    The first candle uses ``high - low`` (no previous close available).
    Subsequent candles use the standard three-way max definition.

    Returns a list the same length as *candles*.  Returns an empty list
    if *candles* is empty.
    """
    if not candles:
        return []

    result: list[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        hl = candles[i].high - candles[i].low
        hc = abs(candles[i].high - prev_close)
        lc = abs(candles[i].low - prev_close)
        result.append(max(hl, hc, lc))
    return result


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Return the latest ATR value using Wilder's smoothing.

    Returns ``None`` if ``len(candles) < period`` (need at least
    *period* candles to seed the SMA).
    """
    if period < 1 or len(candles) < period:
        return None

    tr_values = true_range(candles)

    # Seed: simple moving average of the first `period` TR values.
    avg = sum(tr_values[:period]) / period

    # Wilder smoothing for the remainder.
    for i in range(period, len(tr_values)):
        avg = (avg * (period - 1) + tr_values[i]) / period

    return avg
