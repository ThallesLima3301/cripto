"""Tests for `score_reversal_confirmation` (Block 17).

Replaces the v1 `score_reversal_pattern` (all-or-nothing 10 pts) with
an additive scorer:

    pattern         +5
    rsi_recovery    +3
    high_reclaim    +2
    cap             10  (sum of sub-weights)

These tests pin every cell of the truth table so the breakdown stays
explicit and a future tweak to one sub-weight is easy to verify.
"""

from __future__ import annotations

from crypto_monitor.signals.factors import score_reversal_confirmation


CAP = 10


# ---------- single-component scoring ----------

def test_no_signal_case_scores_zero():
    pts, detail = score_reversal_confirmation(
        detected=False,
        pattern_name=None,
        rsi_recovery=False,
        high_reclaim=False,
        cap=CAP,
    )
    assert pts == 0
    assert detail["points"] == 0
    assert detail["points_pattern"] == 0
    assert detail["points_rsi_recovery"] == 0
    assert detail["points_high_reclaim"] == 0
    assert detail["detected"] is False
    assert detail["pattern"] is None
    assert detail["rsi_recovery"] is False
    assert detail["high_reclaim"] is False


def test_pattern_only_scores_five():
    pts, detail = score_reversal_confirmation(
        detected=True,
        pattern_name="hammer",
        rsi_recovery=False,
        high_reclaim=False,
        cap=CAP,
    )
    assert pts == 5
    assert detail["points_pattern"] == 5
    assert detail["points_rsi_recovery"] == 0
    assert detail["points_high_reclaim"] == 0
    assert detail["pattern"] == "hammer"


def test_rsi_recovery_only_scores_three():
    pts, detail = score_reversal_confirmation(
        detected=False,
        pattern_name=None,
        rsi_recovery=True,
        high_reclaim=False,
        cap=CAP,
    )
    assert pts == 3
    assert detail["points_pattern"] == 0
    assert detail["points_rsi_recovery"] == 3
    assert detail["points_high_reclaim"] == 0
    assert detail["rsi_recovery"] is True


def test_high_reclaim_only_scores_two():
    pts, detail = score_reversal_confirmation(
        detected=False,
        pattern_name=None,
        rsi_recovery=False,
        high_reclaim=True,
        cap=CAP,
    )
    assert pts == 2
    assert detail["points_pattern"] == 0
    assert detail["points_rsi_recovery"] == 0
    assert detail["points_high_reclaim"] == 2
    assert detail["high_reclaim"] is True


# ---------- additive combinations ----------

def test_pattern_plus_rsi_recovery_scores_eight():
    pts, detail = score_reversal_confirmation(
        detected=True,
        pattern_name="bullish_engulfing",
        rsi_recovery=True,
        high_reclaim=False,
        cap=CAP,
    )
    assert pts == 8
    assert detail["points_pattern"] == 5
    assert detail["points_rsi_recovery"] == 3
    assert detail["points_high_reclaim"] == 0


def test_pattern_plus_high_reclaim_scores_seven():
    pts, _ = score_reversal_confirmation(
        detected=True,
        pattern_name="hammer",
        rsi_recovery=False,
        high_reclaim=True,
        cap=CAP,
    )
    assert pts == 7


def test_rsi_recovery_plus_high_reclaim_scores_five():
    pts, _ = score_reversal_confirmation(
        detected=False,
        pattern_name=None,
        rsi_recovery=True,
        high_reclaim=True,
        cap=CAP,
    )
    assert pts == 5


def test_all_three_score_full_ten():
    pts, detail = score_reversal_confirmation(
        detected=True,
        pattern_name="hammer",
        rsi_recovery=True,
        high_reclaim=True,
        cap=CAP,
    )
    assert pts == 10
    assert detail["points_pattern"] == 5
    assert detail["points_rsi_recovery"] == 3
    assert detail["points_high_reclaim"] == 2
    assert detail["points"] == 10


# ---------- cap respects config budget ----------

def test_lower_cap_truncates_total():
    """If the configured cap is below the natural sum, the helper caps."""
    pts, detail = score_reversal_confirmation(
        detected=True,
        pattern_name="hammer",
        rsi_recovery=True,
        high_reclaim=True,
        cap=6,
    )
    assert pts == 6
    # Sub-component points are still reported as their natural values;
    # only the rolled-up `points` reflects the cap.
    assert detail["points_pattern"] == 5
    assert detail["points_rsi_recovery"] == 3
    assert detail["points_high_reclaim"] == 2
    assert detail["points"] == 6
