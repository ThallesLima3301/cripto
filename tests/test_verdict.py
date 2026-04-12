"""Tests for `crypto_monitor.evaluation.verdict.assign_verdict`.

Pure function — each test just passes a return percent and asserts
the label. The thresholds come from the `eval_settings` fixture
which mirrors `config.example.toml` (10 / 5 / -5 / -10).
"""

from __future__ import annotations

from crypto_monitor.evaluation import (
    VERDICT_BAD,
    VERDICT_GOOD,
    VERDICT_GREAT,
    VERDICT_NEUTRAL,
    VERDICT_PENDING,
    VERDICT_POOR,
    assign_verdict,
)


def test_pending_when_return_is_none(eval_settings):
    assert assign_verdict(None, eval_settings) == VERDICT_PENDING


def test_great_at_and_above_threshold(eval_settings):
    assert assign_verdict(10.0, eval_settings) == VERDICT_GREAT
    assert assign_verdict(25.0, eval_settings) == VERDICT_GREAT


def test_good_between_good_and_great(eval_settings):
    assert assign_verdict(5.0, eval_settings) == VERDICT_GOOD
    assert assign_verdict(9.99, eval_settings) == VERDICT_GOOD


def test_neutral_inside_the_noise_band(eval_settings):
    assert assign_verdict(0.0, eval_settings) == VERDICT_NEUTRAL
    assert assign_verdict(4.99, eval_settings) == VERDICT_NEUTRAL
    assert assign_verdict(-4.99, eval_settings) == VERDICT_NEUTRAL


def test_poor_at_and_below_poor_but_above_bad(eval_settings):
    assert assign_verdict(-5.0, eval_settings) == VERDICT_POOR
    assert assign_verdict(-9.99, eval_settings) == VERDICT_POOR


def test_bad_at_and_below_bad_threshold(eval_settings):
    assert assign_verdict(-10.0, eval_settings) == VERDICT_BAD
    assert assign_verdict(-30.0, eval_settings) == VERDICT_BAD
