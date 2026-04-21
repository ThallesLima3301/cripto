"""Tests for Average True Range indicator.

Hand-computed values use a small period (3 or 5) to keep the arithmetic
auditable.  The Wilder smoothing formula is verified step-by-step.
"""

from __future__ import annotations

import pytest

from crypto_monitor.indicators.atr import atr, true_range
from crypto_monitor.indicators.types import Candle


# ---------- helpers ----------

def _c(o: float, h: float, l: float, c: float) -> Candle:
    """Shorthand candle with dummy timestamps."""
    return Candle(
        open_time="2025-01-01T00:00:00Z",
        open=o, high=h, low=l, close=c,
        volume=100.0,
        close_time="2025-01-01T00:59:59Z",
    )


# ---------- true_range ----------

class TestTrueRange:

    def test_empty_list(self):
        assert true_range([]) == []

    def test_single_candle(self):
        """First candle: TR = high - low."""
        candles = [_c(100, 110, 90, 105)]
        assert true_range(candles) == [20.0]  # 110 - 90

    def test_normal_sequence(self):
        """Standard TR computation with prev_close context."""
        candles = [
            _c(100, 110, 95, 105),   # TR[0] = 110 - 95 = 15
            _c(106, 112, 100, 108),  # hl=12, |112-105|=7, |100-105|=5 → 12
            _c(109, 115, 103, 110),  # hl=12, |115-108|=7, |103-108|=5 → 12
        ]
        result = true_range(candles)
        assert result == [15.0, 12.0, 12.0]

    def test_gap_up(self):
        """Gap up: high - prev_close > high - low."""
        candles = [
            _c(100, 105, 95, 100),   # TR[0] = 10
            _c(110, 118, 108, 115),  # hl=10, |118-100|=18, |108-100|=8 → 18
        ]
        result = true_range(candles)
        assert result[0] == 10.0
        assert result[1] == 18.0

    def test_gap_down(self):
        """Gap down: |low - prev_close| > high - low."""
        candles = [
            _c(100, 105, 95, 100),  # TR[0] = 10
            _c(85, 90, 80, 82),     # hl=10, |90-100|=10, |80-100|=20 → 20
        ]
        result = true_range(candles)
        assert result[0] == 10.0
        assert result[1] == 20.0

    def test_flat_candles(self):
        """All OHLC equal → TR = 0 for every candle."""
        candles = [_c(100, 100, 100, 100) for _ in range(5)]
        result = true_range(candles)
        assert result == [0.0, 0.0, 0.0, 0.0, 0.0]

    def test_length_matches_input(self):
        candles = [_c(i, i + 5, i - 5, i + 1) for i in range(20)]
        assert len(true_range(candles)) == 20


# ---------- atr: insufficient data ----------

class TestAtrInsufficientData:

    def test_empty_list(self):
        assert atr([], period=14) is None

    def test_single_candle(self):
        assert atr([_c(100, 110, 90, 105)], period=14) is None

    def test_fewer_than_period(self):
        candles = [_c(100, 110, 90, 105) for _ in range(13)]
        assert atr(candles, period=14) is None

    def test_exactly_period_candles(self):
        """With exactly `period` candles we have enough to compute the SMA seed."""
        candles = [_c(100, 110, 90, 100) for _ in range(14)]
        result = atr(candles, period=14)
        assert result is not None

    def test_period_zero(self):
        assert atr([_c(100, 110, 90, 105)], period=0) is None

    def test_period_negative(self):
        assert atr([_c(100, 110, 90, 105)], period=-1) is None


# ---------- atr: known values (period=3) ----------

class TestAtrKnownValues:
    """Hand-computed ATR with period=3 for full auditability.

    Candles:
      c0: O=100 H=108 L=96  C=104  → TR=12 (first candle: H-L)
      c1: O=105 H=112 L=101 C=110  → hl=11, |112-104|=8, |101-104|=3 → 11
      c2: O=109 H=114 L=106 C=107  → hl=8,  |114-110|=4, |106-110|=4 → 8
      c3: O=108 H=120 L=105 C=118  → hl=15, |120-107|=13, |105-107|=2 → 15
      c4: O=117 H=119 L=113 C=116  → hl=6,  |119-118|=1, |113-118|=5 → 6

    ATR computation (period=3):
      seed = mean(TR[0..2]) = (12 + 11 + 8) / 3 = 31/3 ≈ 10.3333
      ATR after c3: (10.3333 * 2 + 15) / 3 = 35.6667 / 3 ≈ 11.8889
      ATR after c4: (11.8889 * 2 + 6) / 3  = 29.7778 / 3 ≈ 9.9259
    """

    @pytest.fixture
    def candles(self) -> list[Candle]:
        return [
            _c(100, 108, 96, 104),
            _c(105, 112, 101, 110),
            _c(109, 114, 106, 107),
            _c(108, 120, 105, 118),
            _c(117, 119, 113, 116),
        ]

    def test_true_range_values(self, candles):
        tr = true_range(candles)
        assert tr == pytest.approx([12.0, 11.0, 8.0, 15.0, 6.0])

    def test_atr_seed_only(self, candles):
        """period=3, only 3 candles → just the SMA seed."""
        result = atr(candles[:3], period=3)
        assert result == pytest.approx(31.0 / 3)

    def test_atr_one_smoothing_step(self, candles):
        """period=3, 4 candles → seed + one Wilder step."""
        result = atr(candles[:4], period=3)
        seed = 31.0 / 3
        expected = (seed * 2 + 15.0) / 3
        assert result == pytest.approx(expected)

    def test_atr_two_smoothing_steps(self, candles):
        """period=3, all 5 candles → seed + two Wilder steps."""
        result = atr(candles, period=3)
        seed = 31.0 / 3
        step1 = (seed * 2 + 15.0) / 3
        step2 = (step1 * 2 + 6.0) / 3
        assert result == pytest.approx(step2)

    def test_atr_with_period_5(self, candles):
        """period=5, exactly 5 candles → just the seed (SMA of all TRs)."""
        result = atr(candles, period=5)
        expected = (12.0 + 11.0 + 8.0 + 15.0 + 6.0) / 5
        assert result == pytest.approx(expected)


# ---------- atr: Wilder smoothing consistency ----------

class TestWilderSmoothing:
    """Verify the smoothing behavior matches the Wilder formula exactly.

    Wilder's smoothing: ATR_n = (ATR_{n-1} * (period - 1) + TR_n) / period

    This is equivalent to an EMA with alpha = 1/period (not 2/(period+1)
    which is the standard EMA). The RSI module uses the same formula.
    """

    def test_constant_tr_converges_to_tr(self):
        """If every candle has the same TR, ATR should equal that TR value
        regardless of the smoothing phase.
        """
        # All candles: H=110, L=90, C=100 → every TR=20 (gap terms also 20 or less)
        candles = [_c(100, 110, 90, 100) for _ in range(50)]
        result = atr(candles, period=14)
        assert result == pytest.approx(20.0)

    def test_smoothing_decays_old_values(self):
        """After a spike, ATR gradually decays back toward the new lower TR."""
        # 5 high-volatility candles (TR=20) then 20 low-volatility (TR=2)
        high_vol = [_c(100, 110, 90, 100) for _ in range(5)]
        low_vol = [_c(100, 101, 99, 100) for _ in range(20)]
        candles = high_vol + low_vol

        result = atr(candles, period=5)
        # ATR should be much closer to 2 than to 20 after 20 low-vol candles.
        assert result is not None
        assert result < 5.0
        assert result > 2.0  # hasn't fully converged in 20 steps

    def test_smoothing_reacts_to_spike(self):
        """A sudden spike pulls ATR upward, but doesn't jump to the spike TR."""
        low_vol = [_c(100, 102, 98, 100) for _ in range(20)]
        spike = [_c(100, 130, 70, 90)]  # TR = 60
        candles = low_vol + spike

        atr_before = atr(low_vol, period=5)
        atr_after = atr(candles, period=5)

        assert atr_before is not None
        assert atr_after is not None
        # ATR should increase but not jump to 60.
        assert atr_after > atr_before
        assert atr_after < 60.0

    def test_period_1_equals_last_tr(self):
        """With period=1, ATR should equal the last TR value
        (seed is TR[0], then each step fully replaces with the new TR).
        """
        candles = [
            _c(100, 110, 90, 100),   # TR=20
            _c(100, 105, 95, 102),   # TR=10
            _c(102, 108, 98, 106),   # TR=10
        ]
        result = atr(candles, period=1)
        # period=1: seed=TR[0]=20, step1=(20*0+10)/1=10, step2=(10*0+10)/1=10
        assert result == pytest.approx(10.0)

    def test_step_by_step_matches_formula(self):
        """Manually verify each Wilder smoothing step for period=4."""
        candles = [
            _c(100, 106, 94, 102),   # TR=12
            _c(103, 109, 99, 105),   # hl=10, |109-102|=7, |99-102|=3 → 10
            _c(104, 108, 98, 100),   # hl=10, |108-105|=3, |98-105|=7 → 10
            _c(101, 110, 96, 108),   # hl=14, |110-100|=10, |96-100|=4 → 14
            _c(107, 112, 104, 109),  # hl=8, |112-108|=4, |104-108|=4 → 8
            _c(110, 116, 106, 114),  # hl=10, |116-109|=7, |106-109|=3 → 10
        ]
        tr = true_range(candles)
        assert tr == pytest.approx([12.0, 10.0, 10.0, 14.0, 8.0, 10.0])

        # Seed (period=4): mean(12, 10, 10, 14) = 46/4 = 11.5
        seed = 46.0 / 4
        # Step 1: (11.5 * 3 + 8) / 4 = 42.5 / 4 = 10.625
        step1 = (seed * 3 + 8.0) / 4
        # Step 2: (10.625 * 3 + 10) / 4 = 41.875 / 4 = 10.46875
        step2 = (step1 * 3 + 10.0) / 4

        result = atr(candles, period=4)
        assert result == pytest.approx(step2)


# ---------- atr: package-level import ----------

def test_import_from_package():
    """atr and true_range are importable from the indicators package."""
    from crypto_monitor.indicators import atr as atr_fn, true_range as tr_fn
    assert callable(atr_fn)
    assert callable(tr_fn)
