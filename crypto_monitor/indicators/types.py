"""Shared types for the indicators package.

A single tiny `Candle` NamedTuple is the lingua franca for any indicator
that needs OHLC. Indicators that only need a single series (RSI, volume)
take plain `list[float]` so they remain trivially testable.

`Candle` matches the column shape of the `candles` table so the signal
engine can convert sqlite3.Row -> Candle in one comprehension.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


class Candle(NamedTuple):
    open_time: str    # UTC ISO 8601
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: str   # UTC ISO 8601


@dataclass(frozen=True)
class SupportInfo:
    """Result of `find_heuristic_support`."""
    price: float            # the swing-low price treated as support
    distance_pct: float     # how far above support the current price is, in %
    candle_open_time: str   # UTC ISO of the swing-low candle


@dataclass(frozen=True)
class ReversalInfo:
    """Result of `detect_reversal`."""
    detected: bool
    pattern_name: str | None    # 'hammer' | 'bullish_engulfing' | 'doji' | None
