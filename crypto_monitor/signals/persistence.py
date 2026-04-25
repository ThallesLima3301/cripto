"""Persistence + dedup for signal candidates.

Dedup is rule-driven first, DB-constraint second:

  1. `severity is None` → nothing to store (`below_threshold`).
  2. Query the maximum existing severity for (symbol, candle_hour).
  3. No existing row → INSERT (`inserted`).
  4. Existing severity ≥ new severity → SKIP (`duplicate` when equal,
     `superseded` when the existing one is strictly higher).
  5. Existing severity < new severity → INSERT (`escalated`). This is
     the Phase 1 "severity escalation exception": a higher-severity
     signal for the same candle is allowed to fire even though a lower
     one already exists.

The `UNIQUE(symbol, candle_hour, severity)` constraint on the signals
table is retained as a safety net — if two parallel scans ever race,
one of them will catch IntegrityError and return `duplicate`. But the
explicit rule above is what enforces the intent day-to-day, so the
logic is inspectable at the application layer instead of hiding inside
a constraint violation.

This module also exposes `load_candles()` — a narrow read helper used
by the Block 10 scanner and by engine tests. It is intentionally the
only DB-facing utility in this block; wider orchestration (iteration
over symbols, error reporting, logging) will be built on top of this
and the scoring engine when Block 10 lands.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from crypto_monitor.indicators import Candle
from crypto_monitor.signals.types import SignalCandidate


logger = logging.getLogger(__name__)


# Severity ladder. Higher number = more severe. Used by the rule-driven
# dedup check to compare an existing row's severity to a new candidate's.
SEVERITY_RANK: dict[str, int] = {
    "normal": 1,
    "strong": 2,
    "very_strong": 3,
}


# Reason codes for InsertResult. A closed set so callers can branch
# without string typos.
REASON_INSERTED = "inserted"
REASON_ESCALATED = "escalated"
REASON_DUPLICATE = "duplicate"
REASON_SUPERSEDED = "superseded"
REASON_BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True)
class InsertResult:
    """Outcome of an `insert_signal` call.

    `inserted` is True exactly when a new row was written; `reason`
    always carries one of the REASON_* constants above. `signal_id` is
    populated only on successful inserts.
    """
    inserted: bool
    signal_id: int | None
    reason: str


# ---------- candle loading ----------

def load_candles(
    conn: sqlite3.Connection,
    symbol: str,
    interval: str,
    limit: int = 250,
) -> list[Candle]:
    """Load the most recent `limit` closed candles for (symbol, interval).

    Rows are returned in chronological (oldest-first) order so that
    indicator helpers — which assume the last element is the most
    recent — work directly on the result.
    """
    rows = conn.execute(
        """
        SELECT open_time, open, high, low, close, volume, close_time
        FROM candles
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time DESC
        LIMIT ?
        """,
        (symbol, interval, limit),
    ).fetchall()
    return [
        Candle(
            open_time=r["open_time"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            close_time=r["close_time"],
        )
        for r in reversed(rows)
    ]


# ---------- insert + dedup ----------

def insert_signal(
    conn: sqlite3.Connection,
    candidate: SignalCandidate,
) -> InsertResult:
    """Persist a SignalCandidate if the dedup rules allow it.

    See module docstring for the full rule set. The function commits
    only on a successful write; the caller's broader transaction is
    untouched on skip outcomes.
    """
    if candidate.severity is None:
        return InsertResult(
            inserted=False, signal_id=None, reason=REASON_BELOW_THRESHOLD
        )

    existing_max = _existing_max_severity(conn, candidate.symbol, candidate.candle_hour)

    if existing_max is not None:
        existing_rank = SEVERITY_RANK.get(existing_max, 0)
        new_rank = SEVERITY_RANK.get(candidate.severity, 0)
        if existing_rank == new_rank:
            return InsertResult(
                inserted=False, signal_id=None, reason=REASON_DUPLICATE
            )
        if existing_rank > new_rank:
            return InsertResult(
                inserted=False, signal_id=None, reason=REASON_SUPERSEDED
            )
        # existing_rank < new_rank → fall through and insert (escalation)

    reason = REASON_ESCALATED if existing_max is not None else REASON_INSERTED

    try:
        cur = _do_insert(conn, candidate)
    except sqlite3.IntegrityError:
        # Safety net: a parallel writer got here first with the same
        # (symbol, candle_hour, severity) tuple. Report it as a plain
        # duplicate so the caller treats it as a no-op.
        logger.debug(
            "signal unique-constraint race: %s candle_hour=%s severity=%s",
            candidate.symbol, candidate.candle_hour, candidate.severity,
        )
        return InsertResult(
            inserted=False, signal_id=None, reason=REASON_DUPLICATE
        )

    conn.commit()
    return InsertResult(inserted=True, signal_id=cur.lastrowid, reason=reason)


# ---------- internals ----------

def _existing_max_severity(
    conn: sqlite3.Connection, symbol: str, candle_hour: str
) -> str | None:
    """Return the highest-ranked severity already stored for this candle, or None."""
    rows = conn.execute(
        """
        SELECT severity FROM signals
        WHERE symbol = ? AND candle_hour = ?
        """,
        (symbol, candle_hour),
    ).fetchall()
    if not rows:
        return None
    return max(
        (r["severity"] for r in rows),
        key=lambda s: SEVERITY_RANK.get(s, 0),
    )


def _do_insert(
    conn: sqlite3.Connection, candidate: SignalCandidate
) -> sqlite3.Cursor:
    """Execute the raw INSERT for a SignalCandidate."""
    return conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, dominant_trigger_timeframe,
            drop_1h_pct, drop_24h_pct, drop_7d_pct, drop_30d_pct, drop_180d_pct,
            distance_from_30d_high_pct, distance_from_180d_high_pct,
            recent_30d_high, recent_180d_high, drop_trigger_pct,
            rsi_1h, rsi_4h, rel_volume, dist_support_pct, support_level_price,
            reversal_signal, trend_context_4h, trend_context_1d,
            score_breakdown, alerted, alert_skipped_reason,
            regime_at_signal, watchlist_id
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, 0, NULL,
            ?, ?
        )
        """,
        (
            candidate.symbol,
            candidate.detected_at,
            candidate.candle_hour,
            candidate.price_at_signal,
            candidate.score,
            candidate.severity,
            candidate.trigger_reason,
            candidate.dominant_trigger_timeframe,
            candidate.drop_1h_pct,
            candidate.drop_24h_pct,
            candidate.drop_7d_pct,
            candidate.drop_30d_pct,
            candidate.drop_180d_pct,
            candidate.distance_from_30d_high_pct,
            candidate.distance_from_180d_high_pct,
            candidate.recent_30d_high,
            candidate.recent_180d_high,
            candidate.drop_trigger_pct,
            candidate.rsi_1h,
            candidate.rsi_4h,
            candidate.rel_volume,
            candidate.dist_support_pct,
            candidate.support_level_price,
            1 if candidate.reversal_signal else 0,
            candidate.trend_context_4h,
            candidate.trend_context_1d,
            json.dumps(candidate.score_breakdown, sort_keys=True, default=str),
            candidate.regime_at_signal,
            candidate.watchlist_id,
        ),
    )
