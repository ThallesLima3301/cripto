"""Pure watchlist state machine (Block 22).

Given a new score observation and the current watchlist state for a
symbol, :func:`decide_watch_action` returns exactly one of four tags
— it does not touch the database, does not insert signals, does not
notify. The scheduler wiring (a later block) reads the tag and calls
the appropriate ``store`` helper.

State transitions
-----------------

Let ``E`` = regular buy-signal emit threshold
(``scoring.thresholds.min_signal_score`` plus any regime adjust)
and ``F`` = ``watchlist.floor_score``.

======================  ==============================  =============
score vs thresholds      has_active_watch = False        has_active_watch = True
======================  ==============================  =============
``score >= E``           PROMOTE                         PROMOTE
``F <= score < E``       WATCH                           WATCH
``score < F``            IGNORE                          EXPIRE
======================  ==============================  =============

Rationale:
  * PROMOTE is independent of prior state — a qualifying score always
    earns a real signal. When there is an active watch the caller
    links ``signals.watchlist_id`` to it; otherwise a fresh signal
    emits without a watch history.
  * WATCH is idempotent: the caller ``upsert``s whether or not a row
    already exists (the store handles the insert-vs-update).
  * EXPIRE only triggers below-floor *cleanup* when a watch is active;
    an isolated below-floor observation with no open watch is noise
    (IGNORE) — not worth a DB write.
"""

from __future__ import annotations

from typing import Final


# Action tags exposed as module-level constants rather than an Enum to
# keep the surface small and string-friendly (they show up in logs,
# reports, and the ``watchlist.resolution_reason`` column). The
# manager never returns any other value.
WATCH:   Final[str] = "WATCH"
PROMOTE: Final[str] = "PROMOTE"
EXPIRE:  Final[str] = "EXPIRE"
IGNORE:  Final[str] = "IGNORE"

WATCH_ACTIONS: Final[tuple[str, ...]] = (WATCH, PROMOTE, EXPIRE, IGNORE)


def decide_watch_action(
    *,
    score: int,
    min_signal_score: int,
    floor_score: int,
    has_active_watch: bool,
) -> str:
    """Return ``WATCH`` / ``PROMOTE`` / ``EXPIRE`` / ``IGNORE``.

    Parameters
    ----------
    score             — the newly observed score for a symbol.
    min_signal_score  — the regular emit floor (``E`` in the table).
                        Scores at or above promote.
    floor_score       — the watchlist floor (``F``). Scores in
                        ``[floor_score, min_signal_score)`` are the
                        borderline band worth watching.
    has_active_watch  — True when the symbol already has a
                        ``status='watching'`` row.

    Raises
    ------
    ValueError when ``floor_score > min_signal_score`` — the config
    would produce an empty watch band, a misconfiguration the
    scheduler should catch early rather than silently IGNORE-ing
    every observation.
    """
    if floor_score > min_signal_score:
        raise ValueError(
            f"floor_score ({floor_score}) must be <= "
            f"min_signal_score ({min_signal_score})"
        )

    if score >= min_signal_score:
        return PROMOTE
    if score < floor_score:
        return EXPIRE if has_active_watch else IGNORE
    return WATCH
