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
) -> tuple[int, str | None, float | None, dict[str, Any]]:
    """Evaluate drop magnitude across all horizons; return the best.

    `drops` keys: '1h', '24h', '7d', '30d', '180d'. Values are positive
    drop percentages (a rising or flat period scores 0; insufficient
    history is None).

    Returns `(capped_points, dominant_horizon, drop_pct_of_dominant,
    detail)`. When every horizon scored 0, dominant_horizon is None and
    drop_pct_of_dominant is None — the caller uses this to populate
    `dominant_trigger_timeframe` and `drop_trigger_pct`.
    """
    horizon_points: dict[str, int] = {
        "1h":   _tier_up(drops.get("1h"),   th.drop_1h,   th.drop_1h_points),
        "24h":  _tier_up(drops.get("24h"),  th.drop_24h,  th.drop_24h_points),
        "7d":   _tier_up(drops.get("7d"),   th.drop_7d,   th.drop_7d_points),
        "30d":  _tier_up(drops.get("30d"),  th.drop_30d,  th.drop_30d_points),
        "180d": _tier_up(drops.get("180d"), th.drop_180d, th.drop_180d_points),
    }
    best_horizon, best_raw = max(horizon_points.items(), key=lambda kv: kv[1])
    capped = min(best_raw, cap)

    if best_raw == 0:
        return 0, None, None, {"points": 0, "by_horizon": horizon_points}

    return (
        capped,
        best_horizon,
        drops[best_horizon],
        {
            "points": capped,
            "horizon": best_horizon,
            "drop_pct": drops[best_horizon],
            "by_horizon": horizon_points,
        },
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


def score_reversal_pattern(
    detected: bool,
    pattern_name: str | None,
    cap: int,
) -> tuple[int, dict[str, Any]]:
    pts = cap if detected else 0
    return pts, {"points": pts, "detected": detected, "pattern": pattern_name}


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
