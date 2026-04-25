"""Pure indicator functions.

All functions in this package are:
  - pure (no DB access, no I/O)
  - operate on already-fetched closed candles
  - return None / empty / sideways when history is insufficient

The signal engine in `crypto_monitor.signals` is the only consumer.
"""

from crypto_monitor.indicators.atr import atr, true_range
from crypto_monitor.indicators.patterns import (
    detect_high_reclaim,
    detect_reversal,
    detect_rsi_recovery,
    is_bullish_engulfing,
    is_doji,
    is_hammer,
)
from crypto_monitor.indicators.rsi import rsi
from crypto_monitor.indicators.support import find_heuristic_support
from crypto_monitor.indicators.trend import ema, trend_label
from crypto_monitor.indicators.types import Candle, ReversalInfo, SupportInfo
from crypto_monitor.indicators.volume import relative_volume

__all__ = [
    "Candle",
    "SupportInfo",
    "ReversalInfo",
    "atr",
    "true_range",
    "rsi",
    "relative_volume",
    "ema",
    "trend_label",
    "find_heuristic_support",
    "detect_reversal",
    "detect_rsi_recovery",
    "detect_high_reclaim",
    "is_hammer",
    "is_doji",
    "is_bullish_engulfing",
]
