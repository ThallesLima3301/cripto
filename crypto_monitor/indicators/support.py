"""Heuristic support detection.

This is intentionally simple — it is NOT institution-grade market
structure analysis (Phase 1 point 10). It identifies "the closest
recent visible bottom" using swing-low pivots:

  A swing low is a candle whose `low` is strictly less than the lows
  of the `swing_window` candles immediately before AND after it.

Of all swing lows in the lookback range that sit at or below the
current price, we return the one with the highest `low` — i.e. the
closest support from below. The signal engine treats "near support"
as a positive factor for buy signals.

Operates on daily candles to keep the support level coarse and stable;
intraday noise should not move support every hour.
"""

from __future__ import annotations

from crypto_monitor.indicators.types import Candle, SupportInfo


def find_heuristic_support(
    daily_candles: list[Candle],
    current_price: float,
    lookback_days: int = 90,
    swing_window: int = 3,
) -> SupportInfo | None:
    """Return the closest swing-low support at or below `current_price`.

    Returns `None` if there isn't enough history (need at least
    `2 * swing_window + 1` candles) or no swing low sits at-or-below
    the current price within the lookback range.
    """
    min_required = 2 * swing_window + 1
    if len(daily_candles) < min_required or current_price <= 0:
        return None

    # Take the most recent `lookback_days` candles.
    if lookback_days < len(daily_candles):
        relevant = daily_candles[-lookback_days:]
    else:
        relevant = list(daily_candles)

    if len(relevant) < min_required:
        return None

    swing_lows: list[Candle] = []
    for i in range(swing_window, len(relevant) - swing_window):
        candle = relevant[i]
        before = (relevant[j].low for j in range(i - swing_window, i))
        after = (relevant[j].low for j in range(i + 1, i + 1 + swing_window))
        if all(candle.low < b for b in before) and all(candle.low < a for a in after):
            swing_lows.append(candle)

    candidates = [c for c in swing_lows if c.low <= current_price]
    if not candidates:
        return None

    closest = max(candidates, key=lambda c: c.low)
    distance_pct = (current_price - closest.low) / closest.low * 100.0

    return SupportInfo(
        price=closest.low,
        distance_pct=distance_pct,
        candle_open_time=closest.open_time,
    )
