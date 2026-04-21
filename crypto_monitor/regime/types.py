"""Shared types for the regime package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegimeSnapshot:
    """Result of BTC-based market regime classification.

    Stored in the ``regime_snapshots`` table and stamped on each signal
    emitted during the scan cycle that produced the snapshot.
    """
    label: str              # "risk_on" | "neutral" | "risk_off"
    btc_ema_short: float    # EMA(20) of BTC daily closes
    btc_ema_long: float     # EMA(50) of BTC daily closes
    btc_atr_14d: float      # ATR(14) of BTC daily candles
    atr_percentile: float   # 0–100; where current ATR sits in its 90-day range
    determined_at: str       # UTC ISO 8601
