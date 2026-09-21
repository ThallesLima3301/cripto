"""Shared emission floor and severity policy for scoring and watchlists."""

from crypto_monitor.config.settings import ScoringSettings


def effective_emit_floor(
    scoring: ScoringSettings,
    min_score_adjust: int = 0,
) -> int:
    """Apply regime adjustment without a second, unadjusted normal gate.

    A custom normal floor still applies when no regime adjustment is
    present. Negative adjustments relax it alongside the configured
    minimum; positive adjustments raise only the configured minimum.
    """
    return max(
        scoring.thresholds.min_signal_score + min_score_adjust,
        scoring.severity.normal + min(min_score_adjust, 0),
    )


def severity_for_score(
    score: int,
    scoring: ScoringSettings,
    min_score_adjust: int = 0,
) -> str | None:
    """Gate emission once, then apply the unchanged stronger tier limits."""
    if score < effective_emit_floor(scoring, min_score_adjust):
        return None
    if score >= scoring.severity.very_strong:
        return "very_strong"
    if score >= scoring.severity.strong:
        return "strong"
    return "normal"
