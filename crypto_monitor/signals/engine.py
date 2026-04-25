"""Pure signal engine.

`score_signal(...)` takes already-loaded closed candles for the 1h, 4h
and 1d intervals and returns a `SignalCandidate`. It is pure: no DB
access, no I/O, no mutation of inputs. It is safe to call on partial
history — every missing input is treated as a zero-point factor rather
than as an error.

Contract:

  * Returns `SignalCandidate` when `candles_1h` has at least one entry
    (we need a close price and a candle hour to identify the signal).
  * Returns `None` ONLY when there is no usable closed 1h candle data.
  * A candidate below `min_signal_score` is still returned; its
    `severity` is `None` and `should_emit` is False. Callers decide
    whether to persist — this keeps the engine's behavior observable.

The engine is deliberately dumb about timing (`detected_at` defaults to
`now_utc()`, but tests pass a fixed value). The candle_hour used for
dedup comes from the latest closed 1h candle's `open_time`, never from
wall-clock time — per the Block 6 requirement.
"""

from __future__ import annotations

from crypto_monitor.config.settings import ScoringSettings
from crypto_monitor.indicators import (
    Candle,
    atr,
    detect_high_reclaim,
    detect_reversal,
    detect_rsi_recovery,
    find_heuristic_support,
    relative_volume,
    rsi,
    trend_label,
)
from crypto_monitor.signals.factors import (
    score_discount_from_high,
    score_drop_magnitude,
    score_relative_volume,
    score_reversal_confirmation,
    score_rsi_oversold,
    score_support_distance,
    score_trend_context,
)
from crypto_monitor.signals.types import SignalCandidate
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


def score_signal(
    symbol: str,
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    candles_1d: list[Candle],
    scoring: ScoringSettings,
    *,
    detected_at: str | None = None,
    regime_at_signal: str | None = None,
    min_score_adjust: int = 0,
) -> SignalCandidate | None:
    """Score a symbol and return a candidate, or None if no 1h data.

    Parameters added by v2 (backward-compatible defaults):
      *regime_at_signal* — stamped on the candidate for analytics.
      *min_score_adjust* — added to ``min_signal_score`` when computing
        severity. Lets the scheduler raise the bar in risk-off regimes
        (positive value) or lower it in risk-on regimes (negative value)
        without mutating shared config. Has no effect on the raw score
        or on severity tier thresholds; only the emit gate moves.
    """
    if not candles_1h:
        return None

    latest_1h = candles_1h[-1]
    candle_hour = latest_1h.open_time
    price = latest_1h.close

    # ---- prepare indicator inputs (every helper is insufficient-history safe) ----
    drops = _compute_drops(price, candles_1h, candles_1d)
    highs = _compute_highs_and_discounts(price, candles_1d)

    closes_1h = [c.close for c in candles_1h]
    closes_4h = [c.close for c in candles_4h]
    closes_1d = [c.close for c in candles_1d]
    volumes_1h = [c.volume for c in candles_1h]

    rsi_1h_val = rsi(closes_1h, period=14)
    rsi_4h_val = rsi(closes_4h, period=14)
    rel_vol_val = relative_volume(volumes_1h, period=20)
    atr_1h_val = atr(candles_1h, period=14)

    support_info = find_heuristic_support(
        candles_1d,
        price,
        lookback_days=scoring.thresholds.support_lookback_days,
    )

    reversal = detect_reversal(candles_1h)
    rsi_recovery = detect_rsi_recovery(closes_1h, period=14)
    high_reclaim = detect_high_reclaim(candles_1h)
    trend_4h = trend_label(closes_4h)
    trend_1d = trend_label(closes_1d)

    # ---- score each factor (each already capped at its weight) ----
    w = scoring.weights
    th = scoring.thresholds

    drop_pts, dom_tf, drop_trigger_pct, drop_detail = score_drop_magnitude(
        drops, th, w.drop_magnitude,
        atr_1h=atr_1h_val,
        price=price,
    )
    rsi_pts, rsi_detail = score_rsi_oversold(
        rsi_1h_val, rsi_4h_val, th, w.rsi_oversold
    )
    vol_pts, vol_detail = score_relative_volume(
        rel_vol_val, th, w.relative_volume
    )
    sup_pts, sup_detail = score_support_distance(
        support_info.distance_pct if support_info else None,
        support_info.price if support_info else None,
        th,
        w.support_distance,
    )
    disc_pts, disc_detail = score_discount_from_high(
        highs["discount_30d_pct"],
        highs["discount_180d_pct"],
        th,
        w.discount_from_high,
    )
    rev_pts, rev_detail = score_reversal_confirmation(
        reversal.detected,
        reversal.pattern_name,
        rsi_recovery,
        high_reclaim,
        w.reversal_pattern,
    )
    trend_pts, trend_detail = score_trend_context(
        trend_1d, w.trend_context
    )

    total_score = (
        drop_pts + rsi_pts + vol_pts + sup_pts + disc_pts + rev_pts + trend_pts
    )

    severity = _severity_for(total_score, scoring, min_score_adjust)
    trigger_reason = _trigger_reason(
        dom_tf, drop_trigger_pct, rsi_1h_val, rel_vol_val, reversal.pattern_name
    )

    breakdown = {
        "total": total_score,
        "drop_magnitude": drop_detail,
        "rsi_oversold": rsi_detail,
        "relative_volume": vol_detail,
        "support_distance": sup_detail,
        "discount_from_high": disc_detail,
        "reversal_pattern": rev_detail,
        "trend_context": trend_detail,
    }

    return SignalCandidate(
        symbol=symbol,
        candle_hour=candle_hour,
        detected_at=detected_at or to_utc_iso(now_utc()),
        price_at_signal=price,
        score=total_score,
        severity=severity,
        drop_1h_pct=drops.get("1h"),
        drop_24h_pct=drops.get("24h"),
        drop_7d_pct=drops.get("7d"),
        drop_30d_pct=drops.get("30d"),
        drop_180d_pct=drops.get("180d"),
        dominant_trigger_timeframe=dom_tf,
        trigger_reason=trigger_reason,
        drop_trigger_pct=drop_trigger_pct,
        recent_30d_high=highs["high_30d"],
        recent_180d_high=highs["high_180d"],
        distance_from_30d_high_pct=highs["discount_30d_pct"],
        distance_from_180d_high_pct=highs["discount_180d_pct"],
        rsi_1h=rsi_1h_val,
        rsi_4h=rsi_4h_val,
        rel_volume=rel_vol_val,
        dist_support_pct=support_info.distance_pct if support_info else None,
        support_level_price=support_info.price if support_info else None,
        reversal_signal=reversal.detected,
        reversal_pattern=reversal.pattern_name,
        trend_context_4h=trend_4h,
        trend_context_1d=trend_1d,
        score_breakdown=breakdown,
        regime_at_signal=regime_at_signal,
    )


# ---------- derived-input helpers ----------

def _compute_drops(
    price: float,
    candles_1h: list[Candle],
    candles_1d: list[Candle],
) -> dict[str, float | None]:
    """Return positive drop magnitudes for all five horizons.

    The value is the amount prices FELL over the horizon — 12.3 means
    a 12.3% drop. A rising or flat horizon yields 0.0. An unavailable
    horizon (not enough history) yields None, which the factor helper
    turns into a zero-point contribution.
    """
    out: dict[str, float | None] = {
        "1h": None, "24h": None, "7d": None, "30d": None, "180d": None,
    }

    # 1h: intra-candle move of the latest closed 1h candle.
    latest = candles_1h[-1]
    out["1h"] = _positive_drop_pct(latest.open, latest.close)

    # 24h: latest close vs the close 24 1h candles earlier.
    if len(candles_1h) >= 25:
        out["24h"] = _positive_drop_pct(candles_1h[-25].close, price)

    # 7d / 30d / 180d: latest price vs the daily close that many days back.
    def _day_drop(days: int) -> float | None:
        if len(candles_1d) <= days:
            return None
        return _positive_drop_pct(candles_1d[-(days + 1)].close, price)

    out["7d"] = _day_drop(7)
    out["30d"] = _day_drop(30)
    out["180d"] = _day_drop(180)
    return out


def _positive_drop_pct(ref: float, current: float) -> float:
    """Return the positive drop % when current < ref, 0 otherwise."""
    if ref <= 0:
        return 0.0
    change = (current - ref) / ref * 100.0
    return max(-change, 0.0)


def _compute_highs_and_discounts(
    price: float,
    candles_1d: list[Candle],
) -> dict[str, float | None]:
    """Return the recent 30d/180d highs and the discount % to each.

    Discount is the percent BELOW the high (0 when price is at or above
    the high). Insufficient history yields None for that horizon.
    """
    out: dict[str, float | None] = {
        "high_30d": None,
        "high_180d": None,
        "discount_30d_pct": None,
        "discount_180d_pct": None,
    }
    if not candles_1d:
        return out

    def _window_high(days: int) -> float | None:
        window = candles_1d[-days:] if len(candles_1d) >= days else candles_1d
        if not window:
            return None
        return max(c.high for c in window)

    high_30 = _window_high(30)
    high_180 = _window_high(180)
    out["high_30d"] = high_30
    out["high_180d"] = high_180

    if high_30 and high_30 > 0:
        out["discount_30d_pct"] = max((high_30 - price) / high_30 * 100.0, 0.0)
    if high_180 and high_180 > 0:
        out["discount_180d_pct"] = max((high_180 - price) / high_180 * 100.0, 0.0)

    return out


def _severity_for(
    score: int,
    scoring: ScoringSettings,
    min_score_adjust: int = 0,
) -> str | None:
    """Map a numeric score to a severity tier, or None if below threshold.

    The effective emit threshold is ``min_signal_score + min_score_adjust``
    so the scheduler can shift the gate by regime without touching config
    or the tier ladder. Tier boundaries (normal/strong/very_strong) are
    intentionally left untouched — only the emit floor moves.
    """
    s = scoring.severity
    effective_floor = scoring.thresholds.min_signal_score + min_score_adjust
    if score < effective_floor:
        return None
    if score >= s.very_strong:
        return "very_strong"
    if score >= s.strong:
        return "strong"
    if score >= s.normal:
        return "normal"
    return None


def _trigger_reason(
    dominant_tf: str | None,
    drop_pct: float | None,
    rsi_1h_val: float | None,
    rel_vol_val: float | None,
    reversal_pattern: str | None,
) -> str:
    """Render a short human-readable summary of what fired the signal.

    Includes only the factors that actually contributed meaningfully
    (positive drop, RSI at or below the first oversold level, elevated
    volume, detected pattern). Returns a generic placeholder when no
    factor was noteworthy — the candidate is usually below threshold
    in that case anyway.
    """
    parts: list[str] = []
    if dominant_tf and drop_pct is not None and drop_pct > 0:
        parts.append(f"drop_{dominant_tf}=-{drop_pct:.1f}%")
    if rsi_1h_val is not None and rsi_1h_val <= 30:
        parts.append(f"rsi_1h={rsi_1h_val:.0f}")
    if rel_vol_val is not None and rel_vol_val >= 1.5:
        parts.append(f"rel_vol={rel_vol_val:.1f}x")
    if reversal_pattern:
        parts.append(f"pattern={reversal_pattern}")
    return " | ".join(parts) if parts else "low-score evaluation"
