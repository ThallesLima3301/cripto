"""Block 27 — bullish divergence (optional refinement).

Three layers covered here:

  1. ``rsi_series`` — pinned shape (warmup ``None``s, real values
     after ``period`` bars).
  2. ``detect_bullish_divergence`` — pure detector with synthetic
     candle / RSI lists for the documented truth-table cells.
  3. ``score_reversal_confirmation`` — the new ``divergence`` kwarg
     respects the cap and the existing 3-component behavior is
     unchanged when the kwarg defaults to ``False``.

End-to-end coverage that the engine wires the flag through is left to
the existing ``test_signal_engine.py`` since the engine path was
already exercised exhaustively in Block 17 / Block 18; here we only
add a focused engine-level test that asserts the off-by-default flag
doesn't change behavior and the on-flag breakdown surfaces the new
sub-component.
"""

from __future__ import annotations

import dataclasses

import pytest

from crypto_monitor.indicators import (
    Candle,
    detect_bullish_divergence,
    rsi_series,
)
from crypto_monitor.signals.factors import score_reversal_confirmation


# ---------- helpers ----------

def _candle(idx: int, close: float) -> Candle:
    return Candle(
        open_time=f"t-{idx:04d}",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100.0,
        close_time=f"t-{idx:04d}c",
    )


def _candles(closes: list[float]) -> list[Candle]:
    return [_candle(i, c) for i, c in enumerate(closes)]


# ---------- rsi_series ----------

class TestRsiSeries:

    def test_warmup_returns_none(self):
        # 14-period: indices 0..13 are the warmup band.
        closes = [100.0 + i for i in range(20)]
        series = rsi_series(closes, period=14)
        assert len(series) == 20
        for i in range(14):
            assert series[i] is None
        for i in range(14, 20):
            assert series[i] is not None

    def test_constant_series_yields_50(self):
        series = rsi_series([100.0] * 20, period=14)
        # Every non-warmup value is the flat-market 50.
        for v in series[14:]:
            assert v == pytest.approx(50.0)

    def test_strict_uptrend_yields_100(self):
        closes = [100.0 + i for i in range(20)]
        series = rsi_series(closes, period=14)
        # All-up moves -> RSI = 100 (matches scalar `rsi`).
        assert series[-1] == pytest.approx(100.0)

    def test_strict_downtrend_yields_0(self):
        closes = [100.0 - i for i in range(20)]
        series = rsi_series(closes, period=14)
        assert series[-1] == pytest.approx(0.0)

    def test_short_series_returns_all_none(self):
        # period+1 = 15; 5 closes is not enough.
        series = rsi_series([100.0, 99.0, 98.0, 97.0, 96.0], period=14)
        assert series == [None, None, None, None, None]


# ---------- detect_bullish_divergence ----------

class TestDetectBullishDivergence:

    def test_lower_low_with_higher_rsi_low_fires(self):
        # Window 8: split into halves of 4. Older half min close at idx 1,
        # newer half min close at idx 4 (overall index). Newer close is
        # lower (89 < 90); newer RSI is higher (35 > 25) -> divergence.
        closes_tail = [95.0, 90.0, 92.0, 93.0,   # older half
                       91.0, 89.0, 90.0, 92.0]  # newer half (low at idx 5)
        rsi_tail = [40.0, 25.0, 30.0, 28.0,
                    32.0, 35.0, 38.0, 36.0]

        # Pad in front so the indexing in the detector picks up only the
        # last window=8 values regardless of any older history.
        candles = _candles([100.0] * 5 + closes_tail)
        rsi_vals: list[float | None] = [None] * 5 + rsi_tail  # type: ignore[assignment]

        assert detect_bullish_divergence(candles, rsi_vals, window=8) is True

    def test_lower_low_with_lower_rsi_low_does_not_fire(self):
        # Newer low is lower in price but RSI is also lower -> not divergence.
        closes_tail = [95.0, 90.0, 92.0, 93.0,
                       91.0, 89.0, 90.0, 92.0]
        rsi_tail = [40.0, 30.0, 32.0, 28.0,
                    25.0, 20.0, 22.0, 24.0]
        candles = _candles([100.0] * 5 + closes_tail)
        rsi_vals: list[float | None] = [None] * 5 + rsi_tail  # type: ignore[assignment]

        assert detect_bullish_divergence(candles, rsi_vals, window=8) is False

    def test_higher_low_in_price_does_not_fire(self):
        # Newer low is HIGHER in price -> not a "lower low" -> no divergence.
        closes_tail = [95.0, 88.0, 92.0, 93.0,
                       94.0, 90.0, 91.0, 96.0]
        rsi_tail = [30.0, 22.0, 28.0, 30.0,
                    34.0, 35.0, 36.0, 40.0]
        candles = _candles([100.0] * 5 + closes_tail)
        rsi_vals: list[float | None] = [None] * 5 + rsi_tail  # type: ignore[assignment]

        assert detect_bullish_divergence(candles, rsi_vals, window=8) is False

    def test_equal_lows_does_not_fire(self):
        # Strict comparisons: equal price-low or equal RSI-low must NOT fire.
        closes_tail = [95.0, 90.0, 92.0, 93.0,
                       94.0, 90.0, 91.0, 92.0]  # both halves min == 90
        rsi_tail = [30.0, 25.0, 28.0, 30.0,
                    32.0, 35.0, 33.0, 31.0]
        candles = _candles([100.0] * 5 + closes_tail)
        rsi_vals: list[float | None] = [None] * 5 + rsi_tail  # type: ignore[assignment]

        assert detect_bullish_divergence(candles, rsi_vals, window=8) is False

    def test_insufficient_history_returns_false(self):
        # window=14 needs at least 14 candles AND 14 rsi values.
        candles = _candles([100.0 - i for i in range(10)])
        rsi_vals = [None] * 10
        assert detect_bullish_divergence(candles, rsi_vals, window=14) is False

    def test_window_too_small_returns_false(self):
        # window < 4 cannot be split into halves with at least 2 bars each.
        candles = _candles([100.0, 99.0, 98.0])
        rsi_vals = [50.0, 40.0, 35.0]
        assert detect_bullish_divergence(candles, rsi_vals, window=3) is False

    def test_none_rsi_at_pivot_returns_false(self):
        # A None RSI value at either pivot bar disables the signal.
        closes_tail = [95.0, 90.0, 92.0, 93.0,
                       91.0, 89.0, 90.0, 92.0]
        rsi_tail: list[float | None] = [None, None, None, None,
                                        32.0, 35.0, 38.0, 36.0]
        candles = _candles([100.0] * 5 + closes_tail)
        rsi_vals = [None] * 5 + rsi_tail
        assert detect_bullish_divergence(candles, rsi_vals, window=8) is False


# ---------- score_reversal_confirmation with divergence ----------

class TestScoreReversalConfirmationWithDivergence:

    def test_divergence_default_false_preserves_block_17_behavior(self):
        # Same inputs the Block 17 truth table pinned: pattern + rsi.
        pts, detail = score_reversal_confirmation(
            detected=True, pattern_name="hammer",
            rsi_recovery=True, high_reclaim=False,
            cap=10,
        )
        assert pts == 8
        assert detail["points_pattern"] == 5
        assert detail["points_rsi_recovery"] == 3
        assert detail["points_high_reclaim"] == 0
        # Block 27 keys are present but read 0 / False.
        assert detail["points_divergence"] == 0
        assert detail["divergence"] is False

    def test_divergence_only_scores_two(self):
        pts, detail = score_reversal_confirmation(
            detected=False, pattern_name=None,
            rsi_recovery=False, high_reclaim=False,
            cap=10,
            divergence=True,
        )
        assert pts == 2
        assert detail["points_divergence"] == 2
        assert detail["divergence"] is True

    def test_all_four_components_capped_at_ten(self):
        # Natural sum 5+3+2+2 = 12, but cap = 10 truncates.
        pts, detail = score_reversal_confirmation(
            detected=True, pattern_name="hammer",
            rsi_recovery=True, high_reclaim=True,
            cap=10,
            divergence=True,
        )
        assert pts == 10
        # Sub-components still report their natural points; only the
        # rolled-up `points` is capped (mirrors Block 17 contract).
        assert detail["points_pattern"] == 5
        assert detail["points_rsi_recovery"] == 3
        assert detail["points_high_reclaim"] == 2
        assert detail["points_divergence"] == 2
        assert detail["points"] == 10

    def test_lower_cap_truncates_with_divergence(self):
        pts, detail = score_reversal_confirmation(
            detected=False, pattern_name=None,
            rsi_recovery=True, high_reclaim=True,
            cap=4,
            divergence=True,
        )
        # Natural: 0+3+2+2 = 7 -> capped to 4.
        assert pts == 4
        assert detail["points"] == 4
        assert detail["points_divergence"] == 2


# ---------- engine integration: feature flag off vs on ----------

class TestEngineFeatureFlag:
    """Lightweight integration: the engine respects ``divergence_enabled``.

    The engine's broader behavior is covered in test_signal_engine.py.
    Here we only verify (a) flag-off path keeps the Block 17 breakdown
    shape and (b) the new fields surface in ``score_breakdown`` either
    way (always-present per the factor docstring).
    """

    def test_flag_off_breakdown_has_zero_divergence(self, scoring_settings):
        from tests.test_signal_engine import _build_crash_series
        from crypto_monitor.signals import score_signal

        candles_1h, candles_4h, candles_1d = _build_crash_series()
        candidate = score_signal(
            "BTCUSDT",
            candles_1h, candles_4h, candles_1d,
            scoring_settings,
            detected_at="2026-04-25T15:00:00Z",
        )
        assert candidate is not None
        rev = candidate.score_breakdown["reversal_pattern"]
        # Block 27 keys present but inert.
        assert rev["points_divergence"] == 0
        assert rev["divergence"] is False

    def test_flag_on_does_not_break_scoring(self, scoring_settings):
        """Flipping the flag on must not regress the scoring engine.

        We don't assert that divergence fires (the crash series isn't
        crafted to produce one) — only that enabling the flag is safe
        and the breakdown shape stays well-formed.
        """
        from tests.test_signal_engine import _build_crash_series
        from crypto_monitor.signals import score_signal

        flagged = dataclasses.replace(
            scoring_settings,
            thresholds=dataclasses.replace(
                scoring_settings.thresholds,
                divergence_enabled=True,
            ),
        )
        candles_1h, candles_4h, candles_1d = _build_crash_series()
        candidate = score_signal(
            "BTCUSDT",
            candles_1h, candles_4h, candles_1d,
            flagged,
            detected_at="2026-04-25T15:00:00Z",
        )
        assert candidate is not None
        rev = candidate.score_breakdown["reversal_pattern"]
        assert "points_divergence" in rev
        assert "divergence" in rev
        assert isinstance(rev["divergence"], bool)
