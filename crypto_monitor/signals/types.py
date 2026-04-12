"""Shared types for the signal engine.

`SignalCandidate` is the single output of `score_signal`. It is ALWAYS
returned when there is enough data to pin down a price and a candle
hour — even when the total score is below `min_signal_score`. Callers
inspect `should_emit` to decide whether the candidate is worth
persisting / alerting on.

Every field maps 1:1 to a column in the `signals` table except for
`reversal_pattern`, which is kept alongside `reversal_signal` purely for
logging / display: the schema stores only the bool flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SignalCandidate:
    # ---- identity ----
    symbol: str
    candle_hour: str        # open_time of the latest closed 1h candle (UTC ISO)
    detected_at: str        # scan time (UTC ISO)
    price_at_signal: float  # close of the latest closed 1h candle

    # ---- score + severity ----
    score: int
    severity: str | None    # "normal" | "strong" | "very_strong" | None (below threshold)

    # ---- drops (positive magnitude; None = insufficient history) ----
    drop_1h_pct: float | None
    drop_24h_pct: float | None
    drop_7d_pct: float | None
    drop_30d_pct: float | None
    drop_180d_pct: float | None

    # ---- trigger context ----
    dominant_trigger_timeframe: str | None   # "1h" | "24h" | "7d" | "30d" | "180d" | None
    trigger_reason: str                      # short human-readable summary
    drop_trigger_pct: float | None           # drop % of the dominant timeframe

    # ---- long-horizon highs + discounts ----
    recent_30d_high: float | None
    recent_180d_high: float | None
    distance_from_30d_high_pct: float | None
    distance_from_180d_high_pct: float | None

    # ---- momentum / volume / support ----
    rsi_1h: float | None
    rsi_4h: float | None
    rel_volume: float | None
    dist_support_pct: float | None
    support_level_price: float | None

    # ---- pattern / trend ----
    reversal_signal: bool
    reversal_pattern: str | None             # name only, for logs/JSON breakdown
    trend_context_4h: str                    # "uptrend" | "sideways" | "downtrend"
    trend_context_1d: str

    # ---- per-factor breakdown (serialized to JSON on insert) ----
    score_breakdown: dict[str, Any] = field(default_factory=dict)

    @property
    def should_emit(self) -> bool:
        """True when the candidate has a severity (score >= min_signal_score)."""
        return self.severity is not None
