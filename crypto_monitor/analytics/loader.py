"""Database-facing helper for analytics consumers.

The pure aggregator in :mod:`aggregator` accepts a list of dicts;
this module's only job is to load that list from the join of
``signal_evaluations`` and ``signals``, optionally restricting to a
recent window.

Block 26 ships:

  * :data:`ScopeName` literal — ``"all" | "90d" | "30d"``.
  * :func:`load_evaluation_rows` — runs the join SQL with a
    ``signals.detected_at >= cutoff`` filter when the scope is bounded.

Filtering by ``signals.detected_at`` (rather than by
``signal_evaluations.evaluated_at``) reflects the user's intuition:
"how have my decisions in the last X days performed". Because the
maturation window is 30 days, very narrow scopes (e.g. ``30d``) will
typically contain few or no fully-matured rows; the formatter / CLI
treat that as the documented "insufficient data" case.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Final, Literal


ScopeName = Literal["all", "90d", "30d"]


_SCOPE_DAYS: Final[dict[str, int]] = {
    "30d": 30,
    "90d": 90,
}


_SELECT_JOIN = """
    SELECT
        s.score                              AS score,
        s.severity                           AS severity,
        s.regime_at_signal                   AS regime_at_signal,
        s.dominant_trigger_timeframe         AS dominant_trigger_timeframe,
        e.return_7d_pct                      AS return_7d_pct,
        e.max_gain_7d_pct                    AS max_gain_7d_pct,
        e.max_loss_7d_pct                    AS max_loss_7d_pct,
        e.time_to_mfe_hours                  AS time_to_mfe_hours,
        e.time_to_mae_hours                  AS time_to_mae_hours
    FROM signal_evaluations e
    JOIN signals s ON s.id = e.signal_id
"""


def load_evaluation_rows(
    conn: sqlite3.Connection,
    *,
    scope: ScopeName = "all",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return the analytics input rows for the given scope.

    When ``scope`` is ``"30d"`` or ``"90d"`` only signals whose
    ``detected_at`` is within ``now - scope_days`` are returned.
    ``scope="all"`` returns every evaluated signal.

    Each row is a plain dict with the columns the aggregator expects.
    The function never raises on empty windows — it simply returns
    ``[]``.
    """
    if scope not in ("all", "30d", "90d"):
        raise ValueError(f"unsupported scope: {scope!r}")

    if scope == "all":
        rows = conn.execute(_SELECT_JOIN).fetchall()
    else:
        if now is None:
            from crypto_monitor.utils.time_utils import now_utc
            now = now_utc()
        from crypto_monitor.utils.time_utils import to_utc_iso
        cutoff = now - timedelta(days=_SCOPE_DAYS[scope])
        rows = conn.execute(
            _SELECT_JOIN + " WHERE s.detected_at >= ?",
            (to_utc_iso(cutoff),),
        ).fetchall()

    return [dict(r) for r in rows]
