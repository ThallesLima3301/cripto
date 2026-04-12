"""Pure verdict assignment.

Given a return percent and the user's configured thresholds, return
one of the verdict labels below. The mapping is intentionally
explicit: every boundary is a comparison against a named field on
`EvaluationSettings`, so a user who edits `config.toml` gets a
different verdict without touching code.

Ordering rules
--------------
`great` and `good` are both positive-side labels and compared
against their "at least" thresholds (>=). `bad` and `poor` are
negative-side and compared against "at most" thresholds (<=).
Anything that falls between `poor_return_pct` and `good_return_pct`
is `neutral`. A missing return value yields `pending`, which is the
sentinel the buy/signal evaluators use when not enough future data
exists yet.

Invariants the loader should have already enforced:
    bad_return_pct < poor_return_pct < 0 < good_return_pct < great_return_pct
The verdict function does NOT re-check this — it trusts config.
"""

from __future__ import annotations

from crypto_monitor.config.settings import EvaluationSettings


VERDICT_GREAT = "great"
VERDICT_GOOD = "good"
VERDICT_NEUTRAL = "neutral"
VERDICT_POOR = "poor"
VERDICT_BAD = "bad"
VERDICT_PENDING = "pending"


def assign_verdict(
    return_pct: float | None,
    eval_settings: EvaluationSettings,
) -> str:
    """Map a return percent to a verdict label. Pure function.

    `return_pct=None` means "we do not yet have enough future data
    to decide" — the caller uses this when the target window hasn't
    elapsed yet, even though maturation said it should have.
    """
    if return_pct is None:
        return VERDICT_PENDING
    if return_pct >= eval_settings.great_return_pct:
        return VERDICT_GREAT
    if return_pct >= eval_settings.good_return_pct:
        return VERDICT_GOOD
    if return_pct <= eval_settings.bad_return_pct:
        return VERDICT_BAD
    if return_pct <= eval_settings.poor_return_pct:
        return VERDICT_POOR
    return VERDICT_NEUTRAL
