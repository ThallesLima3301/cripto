"""Regression coverage for the emission policy shared by engine and watchlist."""

from dataclasses import replace

import pytest

from crypto_monitor.signals.policy import effective_emit_floor, severity_for_score


@pytest.mark.parametrize(
    ("adjust", "score", "expected"),
    [
        (-5, 44, None),
        (-5, 45, "normal"),
        (-5, 47, "normal"),
        (-5, 49, "normal"),
        (0, 49, None),
        (0, 50, "normal"),
        (5, 50, None),
        (5, 52, None),
        (5, 54, None),
        (5, 55, "normal"),
    ],
)
def test_default_regime_eligibility(scoring_settings, adjust, score, expected):
    """Risk-on admits the newly opened band; risk-off blocks the entire gap."""
    assert severity_for_score(score, scoring_settings, adjust) == expected


@pytest.mark.parametrize(
    ("base_floor", "normal", "adjust", "expected_floor"),
    [
        (50, 50, -5, 45),
        (50, 50, 0, 50),
        (50, 50, 5, 55),
        (50, 60, 0, 60),
        (50, 60, -5, 55),
        (50, 60, 5, 60),
        (50, 60, 15, 65),
        (60, 50, 0, 60),
        (60, 50, -5, 55),
        (60, 50, 5, 65),
    ],
)
def test_shared_floor_respects_custom_normal_and_minimum(
    scoring_settings, base_floor, normal, adjust, expected_floor
):
    """Custom normal floors remain meaningful and every shared boundary emits."""
    scoring = replace(
        scoring_settings,
        thresholds=replace(scoring_settings.thresholds, min_signal_score=base_floor),
        severity=replace(scoring_settings.severity, normal=normal),
    )

    assert effective_emit_floor(scoring, adjust) == expected_floor
    assert severity_for_score(expected_floor - 1, scoring, adjust) is None
    assert severity_for_score(expected_floor, scoring, adjust) is not None


@pytest.mark.parametrize(
    ("score", "expected"),
    [(64, "normal"), (65, "strong"), (79, "strong"), (80, "very_strong")],
)
def test_regime_adjustments_preserve_stronger_tier_boundaries(
    scoring_settings, score, expected
):
    """Moving the eligibility gate does not lower or raise the stronger tiers."""
    for adjust in (-5, 0, 5):
        assert severity_for_score(score, scoring_settings, adjust) == expected


def test_eligibility_can_block_an_otherwise_strong_tier(scoring_settings):
    """A high custom emit floor still takes precedence over severity labels."""
    scoring = replace(
        scoring_settings,
        thresholds=replace(scoring_settings.thresholds, min_signal_score=80),
    )

    assert severity_for_score(79, scoring) is None
    assert severity_for_score(80, scoring) == "very_strong"
    assert severity_for_score(79, scoring, -5) == "strong"
    assert severity_for_score(80, scoring, 5) is None


def test_omitted_adjustment_is_neutral(scoring_settings):
    assert effective_emit_floor(scoring_settings) == 50
    assert severity_for_score(49, scoring_settings) is None
    assert severity_for_score(50, scoring_settings) == "normal"
