"""Tests for `crypto_monitor.signals.engine`.

These tests drive the engine with synthetic closed-candle series chosen
so the score lands deterministically in a specific severity tier.

Block 18 also adds tests for `min_score_adjust` — the keyword the
scheduler uses to shift the emit floor by regime (negative in risk_on,
positive in risk_off, 0 in neutral or when the regime feature is
disabled). The shift only moves the emit gate; tier boundaries are
intentionally untouched.

The canonical "crash" series built by `_build_crash_series` produces
exactly 70 points with config.example weights:

    drop_magnitude    25  (drop_7d ~25.9% -> 25pts, dominant = 7d)
    rsi_oversold      20  (rsi_1h=0 -> 18, rsi_4h=0 -> 10, sum capped)
    relative_volume   15  (rel_vol = 5x -> 15)
    support_distance   0  (monotonic descent, no swing-low pivot)
    discount_from_high 10 (30d discount ~59%, 180d discount 60%)
    reversal_pattern   0
    trend_context      0  (downtrend)
    ---
    total             70  -> severity = "strong"

Adding a hammer on the very last 1h candle contributes 5 points from
the pattern sub-component of the reversal_confirmation factor (Block 17:
pattern=5, rsi_recovery=3, high_reclaim=2, cap=10). The monotonic
downtrend means neither RSI recovery nor high reclaim fire, so the
total reaches 75 — still "strong", not "very_strong". A pure-crash
series cannot light all three confirmation sub-components by design.
"""

from __future__ import annotations

import dataclasses

from crypto_monitor.config.settings import ScoringSeverity, ScoringSettings, ScoringThresholds
from crypto_monitor.indicators import Candle
from crypto_monitor.signals import score_signal


# ---------- candle builders ----------

def _mk_candle(
    idx: int,
    label: str,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
) -> Candle:
    """Build a Candle with sensible OHLC defaults around `close`."""
    open_v = open_ if open_ is not None else close
    high_v = high if high is not None else max(open_v, close)
    low_v = low if low is not None else min(open_v, close)
    return Candle(
        open_time=f"{label}-{idx:04d}",
        open=open_v,
        high=high_v,
        low=low_v,
        close=close,
        volume=volume,
        close_time=f"{label}-{idx:04d}c",
    )


def _build_crash_series() -> tuple[list[Candle], list[Candle], list[Candle]]:
    """Build 1h / 4h / 1d series shaped to score exactly strong = 70."""
    # 1h: 180 flat at 50, 20 strictly descending to 40.0.
    candles_1h: list[Candle] = [_mk_candle(i, "1h", 50.0) for i in range(180)]
    for i in range(20):
        price = 50.0 - (i + 1) * 0.5  # 49.5, 49.0, ..., 40.0
        candles_1h.append(_mk_candle(180 + i, "1h", price))

    # Boost the last 1h candle's volume to 5x the 20-candle baseline.
    last = candles_1h[-1]
    candles_1h[-1] = Candle(
        open_time=last.open_time,
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=500.0,
        close_time=last.close_time,
    )

    # 4h: 60 flat at 50, 20 descending. Drives rsi_4h -> 0.
    candles_4h: list[Candle] = [_mk_candle(i, "4h", 50.0) for i in range(60)]
    for i in range(20):
        price = 50.0 - (i + 1) * 0.5
        candles_4h.append(_mk_candle(60 + i, "4h", price))

    # 1d: 170 flat at 100, 30 descending to 40. Drives the 7d/30d/180d drops,
    # the discounts from high, and the 1d downtrend.
    candles_1d: list[Candle] = [_mk_candle(i, "1d", 100.0) for i in range(170)]
    for i in range(30):
        price = 100.0 - (i + 1) * 2.0  # 98, 96, ..., 40
        candles_1d.append(_mk_candle(170 + i, "1d", price))

    return candles_1h, candles_4h, candles_1d


def _build_flat_series() -> tuple[list[Candle], list[Candle], list[Candle]]:
    """Fully flat series — no drops, neutral RSI, baseline volume."""
    candles_1h = [_mk_candle(i, "1h", 50.0) for i in range(200)]
    candles_4h = [_mk_candle(i, "4h", 50.0) for i in range(80)]
    candles_1d = [_mk_candle(i, "1d", 50.0) for i in range(200)]
    return candles_1h, candles_4h, candles_1d


# ---------- contract: no usable 1h data -> None ----------

def test_no_1h_history_returns_none(scoring_settings):
    assert score_signal("BTCUSDT", [], [], [], scoring_settings) is None


def test_empty_all_intervals_returns_none(scoring_settings):
    # The contract is strict: "None only when there is no usable 1h data".
    # Missing 4h/1d alone must NOT drive the return to None — see the
    # insufficient-history test below.
    assert score_signal("BTCUSDT", [], [], [], scoring_settings) is None


# ---------- strong / very_strong scenarios ----------

def test_strong_scenario(scoring_settings):
    candles_1h, candles_4h, candles_1d = _build_crash_series()
    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )

    assert candidate is not None
    assert candidate.should_emit is True
    assert candidate.severity == "strong"
    assert 65 <= candidate.score < 80

    # The 7d drop dominates in this series.
    assert candidate.dominant_trigger_timeframe == "7d"
    assert candidate.drop_trigger_pct is not None
    assert candidate.drop_trigger_pct > 20.0

    # Trigger reason should be informative, not the generic fallback.
    assert candidate.trigger_reason != "low-score evaluation"
    assert "7d" in candidate.trigger_reason

    # First-class fields should be populated end-to-end.
    assert candidate.price_at_signal == 40.0
    assert candidate.candle_hour == candles_1h[-1].open_time
    assert candidate.detected_at == "2026-04-11T15:00:00Z"
    assert candidate.rsi_1h == 0.0
    assert candidate.rel_volume == 5.0
    assert candidate.trend_context_1d == "downtrend"
    assert candidate.recent_30d_high is not None
    assert candidate.recent_180d_high is not None


def test_crash_with_hammer_lifts_score_via_pattern_subcomponent(scoring_settings):
    """A crash + hammer fires the pattern sub-component (5 pts).

    Block 17 split the 10-point reversal factor into pattern (5) +
    rsi_recovery (3) + high_reclaim (2). A monotonic crash cannot
    trigger the latter two — RSI is pinned at zero and price never
    reclaims a prior high — so the hammer bumps the total by 5 only.
    The candidate stays in the 'strong' tier (≥ 65), but the engine's
    reversal-pattern reporting still surfaces the hammer.
    """
    candles_1h, candles_4h, candles_1d = _build_crash_series()

    # Overwrite the last 1h candle with a hammer while preserving the
    # elevated volume. Body=0.5, lower_wick=1.0, upper_wick=0 — satisfies
    # is_hammer (lower_wick >= 2*body, upper_wick <= 0.25*range). Close
    # stays at 40 so the RSI tail still reads as monotonic down (the
    # last delta remains -0.5). The range is kept close to the baseline
    # per-candle move so ATR-normalized drop scoring (Block 16) does
    # not materially dampen the 7d drop tier.
    last = candles_1h[-1]
    candles_1h[-1] = Candle(
        open_time=last.open_time,
        open=40.5,
        high=40.5,
        low=39.0,
        close=40.0,
        volume=last.volume,
        close_time=last.close_time,
    )

    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )

    assert candidate is not None
    assert candidate.severity == "strong"
    assert 70 <= candidate.score < 80
    assert candidate.reversal_signal is True
    assert candidate.reversal_pattern == "hammer"
    assert "hammer" in candidate.trigger_reason

    # The new sub-component breakdown is recorded in the score breakdown.
    rev = candidate.score_breakdown["reversal_pattern"]
    assert rev["points_pattern"] == 5
    assert rev["points_rsi_recovery"] == 0
    assert rev["points_high_reclaim"] == 0


# ---------- no-signal scenario ----------

def test_no_signal_scenario_still_returns_a_candidate(scoring_settings):
    candles_1h, candles_4h, candles_1d = _build_flat_series()

    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )

    # Contract: candidate is ALWAYS returned when there is usable 1h
    # data. A low score is reflected via severity=None + should_emit=False.
    assert candidate is not None
    assert candidate.should_emit is False
    assert candidate.severity is None
    assert candidate.score < scoring_settings.thresholds.min_signal_score

    # Flat data still fills the identity fields.
    assert candidate.symbol == "BTCUSDT"
    assert candidate.price_at_signal == 50.0
    assert candidate.candle_hour == candles_1h[-1].open_time
    assert candidate.trend_context_1d == "sideways"


# ---------- insufficient history ----------

def test_insufficient_history_is_not_an_error(scoring_settings):
    # Three 1h candles, nothing on 4h or 1d — well below the minimum
    # history every factor would normally like. The engine must NOT
    # raise: every factor that can't be computed contributes zero points.
    candles_1h = [_mk_candle(i, "1h", 50.0) for i in range(3)]

    candidate = score_signal(
        "BTCUSDT",
        candles_1h,
        [],  # no 4h history
        [],  # no 1d history
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )

    # Identity fields come from the only thing we have: the latest 1h candle.
    assert candidate is not None
    assert candidate.symbol == "BTCUSDT"
    assert candidate.candle_hour == candles_1h[-1].open_time
    assert candidate.price_at_signal == 50.0

    # Insufficient-history factors must be exposed as None, not 0 —
    # the distinction matters for downstream display.
    assert candidate.rsi_1h is None
    assert candidate.rsi_4h is None
    assert candidate.rel_volume is None
    assert candidate.drop_24h_pct is None
    assert candidate.drop_7d_pct is None
    assert candidate.drop_30d_pct is None
    assert candidate.drop_180d_pct is None
    assert candidate.recent_30d_high is None
    assert candidate.recent_180d_high is None
    assert candidate.support_level_price is None

    # Trend labels fall back to "sideways" on insufficient history.
    assert candidate.trend_context_4h == "sideways"
    assert candidate.trend_context_1d == "sideways"

    # The score is well below threshold, so the candidate is not emit-able.
    assert candidate.score < scoring_settings.thresholds.min_signal_score
    assert candidate.severity is None
    assert candidate.should_emit is False


# ---------- Block 18: regime-driven min_score_adjust ----------

def _scoring_with_floor(scoring: ScoringSettings, floor: int) -> ScoringSettings:
    """Return a copy of `scoring` with `min_signal_score = floor`.

    Used by the Block 18 tests so the natural crash score (70) sits
    just above or below the configured floor and the effect of
    `min_score_adjust` is easy to read.
    """
    new_th = dataclasses.replace(scoring.thresholds, min_signal_score=floor)
    return dataclasses.replace(scoring, thresholds=new_th)


def _scoring_with_severity_split(scoring: ScoringSettings, *, floor: int, normal: int) -> ScoringSettings:
    """Return a copy with a custom emit floor AND a lower severity.normal.

    The default conftest fixture sets ``min_signal_score == severity.normal``,
    so a *negative* `min_score_adjust` cannot promote a candidate without
    also relaxing the severity ladder. This helper drops `severity.normal`
    below `floor` so the risk_on case is observable end-to-end.
    """
    new_th = dataclasses.replace(scoring.thresholds, min_signal_score=floor)
    new_sev = dataclasses.replace(scoring.severity, normal=normal)
    return dataclasses.replace(scoring, thresholds=new_th, severity=new_sev)


def test_min_score_adjust_default_is_noop(scoring_settings):
    """Omitting `min_score_adjust` reproduces v1 emit-floor behavior."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()
    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )
    assert candidate is not None
    assert candidate.severity == "strong"
    assert candidate.should_emit is True


def test_risk_off_adjust_raises_floor_and_blocks_borderline_signal(scoring_settings):
    """A positive adjust raises the emit floor; a 70-pt signal can be suppressed."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()

    # Floor 65 + adjust +10 -> effective floor 75 > score 70 -> blocked.
    cfg = _scoring_with_floor(scoring_settings, floor=65)
    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        cfg,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=10,
    )
    assert candidate is not None
    assert candidate.score == 70
    assert candidate.severity is None
    assert candidate.should_emit is False


def test_risk_off_adjust_below_score_still_emits(scoring_settings):
    """If the raised floor is still below the score, severity is unchanged."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()

    # Floor 50 + adjust +5 -> effective floor 55 < score 70 -> still emits,
    # tier ladder untouched -> severity stays "strong".
    candidate = score_signal(
        "BTCUSDT",
        candles_1h, candles_4h, candles_1d,
        scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=5,
    )
    assert candidate is not None
    assert candidate.score == 70
    assert candidate.severity == "strong"
    assert candidate.should_emit is True


def test_neutral_adjust_zero_matches_baseline(scoring_settings):
    """`min_score_adjust=0` produces the exact same outcome as omitting it."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()
    baseline = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
    )
    explicit = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=0,
    )
    assert baseline is not None and explicit is not None
    assert baseline.score == explicit.score
    assert baseline.severity == explicit.severity
    assert baseline.should_emit == explicit.should_emit


def test_risk_on_adjust_lowers_floor_and_promotes_borderline_signal(scoring_settings):
    """A negative adjust drops the emit floor; a sub-floor score can promote."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()

    # Custom config: floor=80 (so 70-pt crash is normally blocked) but
    # severity.normal=50 (so 70 still maps to a tier when emitted).
    cfg = _scoring_with_severity_split(scoring_settings, floor=80, normal=50)

    blocked = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, cfg,
        detected_at="2026-04-11T15:00:00Z",
    )
    assert blocked is not None
    assert blocked.score == 70
    assert blocked.severity is None
    assert blocked.should_emit is False

    promoted = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, cfg,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=-15,  # effective floor 65 < 70 -> emits
    )
    assert promoted is not None
    assert promoted.score == 70
    assert promoted.severity == "strong"  # tier ladder unchanged
    assert promoted.should_emit is True


def test_min_score_adjust_does_not_change_score_or_breakdown(scoring_settings):
    """The adjust shifts the emit gate only — never the raw score or details."""
    candles_1h, candles_4h, candles_1d = _build_crash_series()
    a = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=0,
    )
    b = score_signal(
        "BTCUSDT", candles_1h, candles_4h, candles_1d, scoring_settings,
        detected_at="2026-04-11T15:00:00Z",
        min_score_adjust=10,
    )
    assert a is not None and b is not None
    assert a.score == b.score
    assert a.score_breakdown == b.score_breakdown
    assert a.trigger_reason == b.trigger_reason
    assert a.dominant_trigger_timeframe == b.dominant_trigger_timeframe
