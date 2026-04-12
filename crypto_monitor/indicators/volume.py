"""Relative volume.

Compares the most recent candle's volume to the mean volume of the
previous N candles. A value of 2.0 means the latest candle had twice
the average volume of the prior `period` candles — a sign of unusual
interest at the current price.

Returns `None` if there aren't at least `period + 1` data points or if
the baseline mean is zero.
"""

from __future__ import annotations


def relative_volume(volumes: list[float], period: int = 20) -> float | None:
    if period < 1 or len(volumes) < period + 1:
        return None

    recent = volumes[-1]
    baseline_window = volumes[-(period + 1):-1]
    baseline = sum(baseline_window) / period

    if baseline <= 0:
        return None

    return recent / baseline
