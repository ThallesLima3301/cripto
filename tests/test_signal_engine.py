"""Tests for `crypto_monitor.signals.engine`.

These tests drive the engine with synthetic closed-candle series chosen
so the score lands deterministically in a specific severity tier.

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

Adding a hammer on the very last 1h candle adds the reversal_pattern
factor's 10 points and bumps the total to 80 -> "very_strong".
"""

from __future__ import annotations

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


def test_very_strong_scenario(scoring_settings):
    candles_1h, candles_4h, candles_1d = _build_crash_series()

    # Overwrite the last 1h candle with a hammer while preserving the
    # elevated volume. Body=1, lower_wick=2.5, upper_wick=0 — a textbook
    # hammer. Close stays at 40 so the RSI tail still reads as monotonic
    # down (the last delta remains -0.5).
    last = candles_1h[-1]
    candles_1h[-1] = Candle(
        open_time=last.open_time,
        open=41.0,
        high=41.0,
        low=37.5,
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
    assert candidate.severity == "very_strong"
    assert candidate.score >= 80
    assert candidate.reversal_signal is True
    assert candidate.reversal_pattern == "hammer"
    assert "hammer" in candidate.trigger_reason


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
