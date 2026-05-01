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


# ---------- read-only helpers (used by the dashboard API) ----------

def count_signals_since(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
) -> int:
    """Return the number of rows in ``signals`` with ``detected_at >= since_iso``.

    Used by the dashboard ``/api/overview`` endpoint to populate the
    "signals in last 24h / 7d" KPI cards. Kept as a small, focused
    reader in this module so the API never embeds raw SQL.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM signals WHERE detected_at >= ?",
        (since_iso,),
    ).fetchone()
    return int(row["cnt"]) if row is not None else 0


def latest_candle_close_time(
    conn: sqlite3.Connection,
    *,
    interval: str = "1h",
) -> str | None:
    """Return the ``close_time`` of the most recent candle for ``interval``.

    Used by the dashboard health probe as a freshness indicator —
    "how stale is the data the API is serving". ``None`` when there
    are no candles yet for that interval.
    """
    row = conn.execute(
        """
        SELECT close_time FROM candles
        WHERE interval = ?
        ORDER BY open_time DESC
        LIMIT 1
        """,
        (interval,),
    ).fetchone()
    return str(row["close_time"]) if row is not None else None


def list_recent_signals(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Return the most recent ``signals`` rows for the activity feed.

    Returns ``sqlite3.Row`` objects so callers can pick the columns
    they care about without the helper having to know about every
    consumer's display shape. Newest first; ties broken by id DESC for
    determinism.
    """
    if limit <= 0:
        return []
    return conn.execute(
        """
        SELECT id, symbol, severity, score, detected_at,
               trigger_reason
        FROM signals
        ORDER BY detected_at DESC, id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


# Columns surfaced on every dashboard signals list / detail row.
# Kept as a constant so ``list_signals`` and ``get_signal_detail``
# stay byte-identical in the columns they project.
_SIGNAL_LIST_COLS = (
    "id, symbol, detected_at, candle_hour, price_at_signal, "
    "score, severity, trigger_reason, "
    "dominant_trigger_timeframe, drop_trigger_pct, "
    "rsi_1h, rsi_4h, rel_volume, "
    "regime_at_signal, watchlist_id"
)


def list_signals(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    severity: str | None = None,
    regime: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Filter + paginate the ``signals`` table for the dashboard.

    All filters are AND'd. ``since_iso`` is ``detected_at >= ?``;
    ``until_iso`` is ``detected_at < ?`` (half-open) so a window
    ``[from, to)`` doesn't double-count the boundary tick.

    Newest first by ``detected_at`` with ``id`` as the tie-breaker
    so paginated reads are deterministic across cycles.
    """
    clauses, params = _signal_filter_clauses(
        symbol=symbol, severity=severity, regime=regime,
        since_iso=since_iso, until_iso=until_iso,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {_SIGNAL_LIST_COLS} FROM signals {where} "
        "ORDER BY detected_at DESC, id DESC "
        "LIMIT ? OFFSET ?"
    )
    params.extend([max(1, int(limit)), max(0, int(offset))])
    return conn.execute(sql, tuple(params)).fetchall()


def count_signals(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    severity: str | None = None,
    regime: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
) -> int:
    """Count signals matching the same filters as :func:`list_signals`."""
    clauses, params = _signal_filter_clauses(
        symbol=symbol, severity=severity, regime=regime,
        since_iso=since_iso, until_iso=until_iso,
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM signals {where}",
        tuple(params),
    ).fetchone()
    return int(row["cnt"]) if row is not None else 0


def get_signal_detail(
    conn: sqlite3.Connection,
    signal_id: int,
) -> sqlite3.Row | None:
    """Return one signal joined with its evaluation row, or ``None``.

    The evaluation columns come from a ``LEFT JOIN`` so signals that
    haven't matured yet still return a row — the evaluation fields are
    simply ``None``. The dashboard's signal-detail page uses this
    single query instead of two round trips.

    Includes the full ``score_breakdown`` JSON column so the
    frontend can render the per-factor breakdown without a second
    fetch. Every column is qualified with its table alias because
    ``signal_evaluations`` shares names like ``id``/``signal_id``
    with ``signals`` — leaving them unqualified would raise
    ``ambiguous column`` at query time.
    """
    return conn.execute(
        """
        SELECT
            s.id                           AS id,
            s.symbol                       AS symbol,
            s.detected_at                  AS detected_at,
            s.candle_hour                  AS candle_hour,
            s.price_at_signal              AS price_at_signal,
            s.score                        AS score,
            s.severity                     AS severity,
            s.trigger_reason               AS trigger_reason,
            s.dominant_trigger_timeframe   AS dominant_trigger_timeframe,
            s.drop_trigger_pct             AS drop_trigger_pct,
            s.rsi_1h                       AS rsi_1h,
            s.rsi_4h                       AS rsi_4h,
            s.rel_volume                   AS rel_volume,
            s.regime_at_signal             AS regime_at_signal,
            s.watchlist_id                 AS watchlist_id,
            s.drop_24h_pct                 AS drop_24h_pct,
            s.drop_7d_pct                  AS drop_7d_pct,
            s.drop_30d_pct                 AS drop_30d_pct,
            s.drop_180d_pct                AS drop_180d_pct,
            s.distance_from_30d_high_pct   AS distance_from_30d_high_pct,
            s.distance_from_180d_high_pct  AS distance_from_180d_high_pct,
            s.dist_support_pct             AS dist_support_pct,
            s.support_level_price          AS support_level_price,
            s.reversal_signal              AS reversal_signal,
            s.trend_context_4h             AS trend_context_4h,
            s.trend_context_1d             AS trend_context_1d,
            s.score_breakdown              AS score_breakdown,
            e.evaluated_at                 AS eval_evaluated_at,
            e.return_24h_pct               AS eval_return_24h_pct,
            e.return_7d_pct                AS eval_return_7d_pct,
            e.return_30d_pct               AS eval_return_30d_pct,
            e.max_gain_7d_pct              AS eval_max_gain_7d_pct,
            e.max_loss_7d_pct              AS eval_max_loss_7d_pct,
            e.time_to_mfe_hours            AS eval_time_to_mfe_hours,
            e.time_to_mae_hours            AS eval_time_to_mae_hours,
            e.verdict                      AS eval_verdict
        FROM signals s
        LEFT JOIN signal_evaluations e ON e.signal_id = s.id
        WHERE s.id = ?
        """,
        (int(signal_id),),
    ).fetchone()


def latest_close_for_symbol(
    conn: sqlite3.Connection,
    symbol: str,
    *,
    interval: str = "1h",
) -> tuple[float, str] | None:
    """Return ``(close, close_time)`` for the latest candle, or ``None``.

    Used by the dashboard ``/api/open-buys`` endpoint as a freshness-
    aware "current price" approximation. ntfy / scheduler code already
    uses the same idiom internally; lifting it to the persistence
    layer means the API never embeds the SQL.
    """
    row = conn.execute(
        """
        SELECT close, close_time FROM candles
        WHERE symbol = ? AND interval = ?
        ORDER BY open_time DESC
        LIMIT 1
        """,
        (symbol, interval),
    ).fetchone()
    if row is None:
        return None
    return (float(row["close"]), str(row["close_time"]))


def _signal_filter_clauses(
    *,
    symbol: str | None,
    severity: str | None,
    regime: str | None,
    since_iso: str | None,
    until_iso: str | None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    if regime is not None:
        clauses.append("regime_at_signal = ?")
        params.append(regime)
    if since_iso is not None:
        clauses.append("detected_at >= ?")
        params.append(since_iso)
    if until_iso is not None:
        clauses.append("detected_at < ?")
        params.append(until_iso)
    return clauses, params


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
