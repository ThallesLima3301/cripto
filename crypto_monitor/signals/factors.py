"""Pure factor helpers for the signal engine.

Every public helper takes already-computed indicator inputs and returns
`(points, detail_dict)`:

  * `points` is an int already capped at the factor's configured weight.
  * `detail_dict` is a JSON-serializable record of inputs and the points
    earned, destined for the `signals.score_breakdown` column.

No helper raises on insufficient data — a missing input yields zero
points and a minimal detail dict. This is the Phase 1 contract: the
engine never aborts scoring because one factor was unavailable.

The tier-lookup logic lives in three small private helpers:

  * `_tier_up`: ascending thresholds + ascending points (drops, volume,
    discounts).
  * `_tier_down`: descending thresholds + ascending points (RSI: lower
    RSI = more points).
  * `_tier_first_match`: ascending thresholds + descending points
    (support distance: the first tier you fall into wins).
"""

from __future__ import annotations

from typing import Any

from crypto_monitor.config.settings import ScoringThresholds


# ---------- private tier helpers ----------

def _tier_up(
    value: float | None,
    thresholds: tuple[float, ...],
    points: tuple[int, ...],
) -> int:
    """Ascending-threshold tiering. Higher value = more points.

    Walks thresholds in order and keeps the highest points whose
    threshold the value meets.
    """
    if value is None:
        return 0
    pts = 0
    for t, p in zip(thresholds, points):
        if value >= t:
            pts = p
    return pts


def _tier_down(
    value: float | None,
    thresholds: tuple[float, ...],
    points: tuple[int, ...],
) -> int:
    """Descending-threshold tiering. Lower value = more points.

    Thresholds are listed highest-first (e.g., RSI [30, 25, 20]); points
    are ascending ([12, 15, 18]). Walks in order and keeps the highest
    points whose threshold the value is still at or below.
    """
    if value is None:
        return 0
    pts = 0
    for t, p in zip(thresholds, points):
        if value <= t:
            pts = p
    return pts


def _tier_first_match(
    value: float | None,
    thresholds: tuple[float, ...],
    points: tuple[int, ...],
) -> int:
    """Ascending-threshold tiering where the FIRST match wins.

    Used for support distance: thresholds ascending [0.5, 1.5, 3, 5],
    points descending [15, 12, 8, 4]. A distance of 0.4 falls in the
    0.5 bucket (15 pts); a distance of 2 falls in the 3 bucket (8 pts).
    """
    if value is None or value < 0:
        return 0
    for t, p in zip(thresholds, points):
        if value <= t:
            return p
    return 0


# ---------- public factor helpers ----------

def score_drop_magnitude(
    drops: dict[str, float | None],
    th: ScoringThresholds,
    cap: int,
    *,
    atr_1h: float | None = None,
    price: float | None = None,
) -> tuple[int, str | None, float | None, dict[str, Any]]:
    """Evaluate drop magnitude across all horizons; return the best.

    `drops` keys: '1h', '24h', '7d', '30d', '180d'. Values are positive
    drop percentages (a rising or flat period scores 0; insufficient
    history is None).

    Returns `(capped_points, dominant_horizon, drop_pct_of_dominant,
    detail)`. When every horizon scored 0, dominant_horizon is None and
    drop_pct_of_dominant is None — the caller uses this to populate
    `dominant_trigger_timeframe` and `drop_trigger_pct`.

    ATR-aware scoring (v2)
    ----------------------
    When both ``atr_1h`` and ``price`` are valid (positive numbers),
    raw drop percentages are divided by ``atr_pct = atr_1h / price * 100``
    before tier lookup. The same config thresholds are then interpreted
    as "drop in ATR units", which amplifies drops in quiet markets and
    dampens them in volatile ones.

    When ATR data is missing, zero, or otherwise invalid, the helper
    falls back to v1 raw-drop behavior exactly — no tier thresholds
    change, no caller needs updating.

    The `drop_pct_of_dominant` return value and the `detail["drop_pct"]`
    field are always the RAW drop percentage — downstream callers
    (alerts, weekly summaries, DB column `drop_trigger_pct`) keep
    their existing meaning even when scoring is normalized.
    """
    atr_normalized = (
        atr_1h is not None
        and atr_1h > 0
        and price is not None
        and price > 0
    )
    atr_pct: float | None = None
    effective_drops: dict[str, float | None] = drops
    if atr_normalized:
        atr_pct = (atr_1h / price) * 100.0
        if atr_pct > 0:
            effective_drops = {
                h: (d / atr_pct if d is not None else None)
                for h, d in drops.items()
            }
        else:
            # Defensive: atr_pct rounded to 0 for a vanishingly small
            # ATR relative to price. Fall back to raw behavior.
            atr_normalized = False
            atr_pct = None

    horizon_points: dict[str, int] = {
        "1h":   _tier_up(effective_drops.get("1h"),   th.drop_1h,   th.drop_1h_points),
        "24h":  _tier_up(effective_drops.get("24h"),  th.drop_24h,  th.drop_24h_points),
        "7d":   _tier_up(effective_drops.get("7d"),   th.drop_7d,   th.drop_7d_points),
        "30d":  _tier_up(effective_drops.get("30d"),  th.drop_30d,  th.drop_30d_points),
        "180d": _tier_up(effective_drops.get("180d"), th.drop_180d, th.drop_180d_points),
    }
    best_horizon, best_raw = max(horizon_points.items(), key=lambda kv: kv[1])
    capped = min(best_raw, cap)

    if best_raw == 0:
        detail: dict[str, Any] = {
            "points": 0,
            "by_horizon": horizon_points,
            "atr_normalized": atr_normalized,
        }
        if atr_pct is not None:
            detail["atr_pct"] = atr_pct
        return 0, None, None, detail

    detail = {
        "points": capped,
        "horizon": best_horizon,
        "drop_pct": drops[best_horizon],
        "by_horizon": horizon_points,
        "atr_normalized": atr_normalized,
    }
    if atr_pct is not None:
        detail["atr_pct"] = atr_pct
    return (
        capped,
        best_horizon,
        drops[best_horizon],
        detail,
    )


def score_rsi_oversold(
    rsi_1h: float | None,
    rsi_4h: float | None,
    th: ScoringThresholds,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    """Sum the 1h + 4h RSI oversold contributions, capped at `cap`.

    The 1h and 4h tiers are additive (oversold on both timeframes is
    stronger than oversold on one) but capped at the factor weight so
    a user cannot blow past the budget by editing config.
    """
    p_1h = _tier_down(rsi_1h, th.rsi_1h_levels, th.rsi_1h_points)
    p_4h = _tier_down(rsi_4h, th.rsi_4h_levels, th.rsi_4h_points)
    total = min(p_1h + p_4h, cap)
    return total, {
        "points": total,
        "points_1h": p_1h,
        "points_4h": p_4h,
        "rsi_1h": rsi_1h,
        "rsi_4h": rsi_4h,
    }


def score_relative_volume(
    rel_vol: float | None,
    th: ScoringThresholds,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    raw = _tier_up(rel_vol, th.rel_volume_levels, th.rel_volume_points)
    pts = min(raw, cap)
    return pts, {"points": pts, "rel_volume": rel_vol}


def score_support_distance(
    distance_pct: float | None,
    support_price: float | None,
    th: ScoringThresholds,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    raw = _tier_first_match(
        distance_pct, th.support_distance_levels, th.support_distance_points
    )
    pts = min(raw, cap)
    return pts, {
        "points": pts,
        "distance_pct": distance_pct,
        "support_price": support_price,
    }


def score_discount_from_high(
    discount_30d_pct: float | None,
    discount_180d_pct: float | None,
    th: ScoringThresholds,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    p_30 = _tier_up(discount_30d_pct, th.discount_30d_levels, th.discount_30d_points)
    p_180 = _tier_up(discount_180d_pct, th.discount_180d_levels, th.discount_180d_points)
    total = min(p_30 + p_180, cap)
    return total, {
        "points": total,
        "points_30d": p_30,
        "points_180d": p_180,
        "discount_30d_pct": discount_30d_pct,
        "discount_180d_pct": discount_180d_pct,
    }


def score_reversal_confirmation(
    detected: bool,
    pattern_name: str | None,
    rsi_recovery: bool,
    high_reclaim: bool,
    cap: int,
    *,
    divergence: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Score candlestick pattern + RSI recovery + high reclaim (additive).

    Sub-weights (Block 17 baseline, Block 27 added the optional
    divergence component):

      * candlestick pattern detected     +5
      * RSI recovered from oversold      +3
      * latest close reclaims prior high +2
      * bullish divergence (Block 27)    +2  (only set when the
        feature flag is on at the call site; default ``False`` keeps
        Block 17 behavior bit-for-bit)

    The sum is capped at ``cap`` (normally the factor budget from
    ``ScoringWeights.reversal_pattern``, which is 10 by default).
    Adding the divergence sub-component does NOT grow the cap — when
    every sub-signal fires the natural sum is 12 but the helper
    returns ``min(..., cap)`` so the factor stays inside its budget.

    The detail dict always reports every sub-component so the breakdown
    remains explicit and testable even when a component scored zero.
    The ``points_divergence`` and ``divergence`` keys are always
    present (they read 0 / False when the caller didn't enable the
    feature), keeping the row shape stable across config flips.
    """
    p_pattern = 5 if detected else 0
    p_rsi = 3 if rsi_recovery else 0
    p_high = 2 if high_reclaim else 0
    p_div = 2 if divergence else 0
    total = min(p_pattern + p_rsi + p_high + p_div, cap)
    return total, {
        "points": total,
        "points_pattern": p_pattern,
        "points_rsi_recovery": p_rsi,
        "points_high_reclaim": p_high,
        "points_divergence": p_div,
        "detected": detected,
        "pattern": pattern_name,
        "rsi_recovery": rsi_recovery,
        "high_reclaim": high_reclaim,
        "divergence": divergence,
    }


# Ladder for the 1d trend factor. Uptrend is rewarded (buying a dip in
# a rising market is safer than buying into a falling knife); sideways
# is neutral-positive; downtrend gets nothing.
_TREND_1D_POINTS: dict[str, int] = {
    "uptrend": 5,
    "sideways": 3,
    "downtrend": 0,
}


def score_trend_context(
    trend_1d: str,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    raw = _TREND_1D_POINTS.get(trend_1d, 0)
    pts = min(raw, cap)
    return pts, {"points": pts, "trend_1d": trend_1d}
