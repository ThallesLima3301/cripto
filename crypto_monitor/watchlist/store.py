"""Persistence helpers for the watchlist (Block 22).

The schema invariant enforced by migration 004 is *one row per symbol
with ``status='watching'`` at a time* (partial unique index). Resolved
rows (``status='promoted'`` or ``status='expired'``) stay in the table
as an audit trail — that's why every "expire" helper transitions the
existing row instead of deleting it.

No side effects outside the ``watchlist`` table. The state machine
that decides *which* helper to call lives in :mod:`manager`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from crypto_monitor.utils.time_utils import to_utc_iso


# ---------- row view ----------

@dataclass(frozen=True)
class WatchlistEntry:
    """A row from the ``watchlist`` table."""
    id: int
    symbol: str
    status: str                       # 'watching' | 'promoted' | 'expired'
    first_seen_at: str                # UTC ISO
    last_seen_at: str                 # UTC ISO
    last_score: int
    expires_at: str                   # UTC ISO
    promoted_signal_id: int | None
    resolved_at: str | None
    resolution_reason: str | None     # 'promoted' | 'expired_stale' | 'expired_below_floor'


# ---------- upsert ----------

def upsert_watching(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    score: int,
    now: datetime,
    max_watch_hours: int,
) -> WatchlistEntry:
    """Insert or refresh the active watch row for ``symbol``.

    When no ``status='watching'`` row exists, inserts a fresh one with
    ``first_seen_at = now`` and ``expires_at = now + max_watch_hours``.
    When one already exists, updates ``last_seen_at``, ``last_score``,
    and extends ``expires_at`` so the watch stays open while the
    borderline score keeps reappearing.

    Returns the up-to-date :class:`WatchlistEntry`.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_watch_hours <= 0:
        raise ValueError("max_watch_hours must be > 0")

    now_iso = to_utc_iso(now)
    expires_iso = to_utc_iso(now + timedelta(hours=max_watch_hours))

    existing = get_watching(conn, symbol=symbol)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO watchlist (
                symbol, status, first_seen_at, last_seen_at,
                last_score, expires_at
            ) VALUES (?, 'watching', ?, ?, ?, ?)
            """,
            (symbol, now_iso, now_iso, int(score), expires_iso),
        )
        conn.commit()
        row_id = int(cur.lastrowid)
    else:
        conn.execute(
            """
            UPDATE watchlist
            SET last_seen_at = ?, last_score = ?, expires_at = ?
            WHERE id = ?
            """,
            (now_iso, int(score), expires_iso, existing.id),
        )
        conn.commit()
        row_id = existing.id

    refreshed = _row_by_id(conn, row_id)
    assert refreshed is not None
    return refreshed


# ---------- promote / expire ----------

def promote(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    signal_id: int,
    now: datetime,
) -> WatchlistEntry | None:
    """Mark the active watch for ``symbol`` as ``promoted``.

    Returns the resolved entry (``status='promoted'``) or ``None``
    when no active watch exists — the scheduler can still insert a
    signal, it simply won't carry a ``watchlist_id`` link.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    existing = get_watching(conn, symbol=symbol)
    if existing is None:
        return None

    conn.execute(
        """
        UPDATE watchlist
        SET status = 'promoted',
            promoted_signal_id = ?,
            resolved_at = ?,
            resolution_reason = 'promoted'
        WHERE id = ?
        """,
        (int(signal_id), to_utc_iso(now), existing.id),
    )
    conn.commit()
    return _row_by_id(conn, existing.id)


def expire_stale(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> int:
    """Expire every active watch whose ``expires_at <= now``.

    Returns the number of rows transitioned.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_iso = to_utc_iso(now)

    cur = conn.execute(
        """
        UPDATE watchlist
        SET status = 'expired',
            resolved_at = ?,
            resolution_reason = 'expired_stale'
        WHERE status = 'watching' AND expires_at <= ?
        """,
        (now_iso, now_iso),
    )
    conn.commit()
    return cur.rowcount or 0


def expire_below_floor(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    now: datetime,
) -> bool:
    """Expire the active watch for ``symbol`` because its score fell.

    Returns True when a row was expired, False when there was no
    active watch to expire (caller treats this as IGNORE upstream).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    existing = get_watching(conn, symbol=symbol)
    if existing is None:
        return False

    conn.execute(
        """
        UPDATE watchlist
        SET status = 'expired',
            resolved_at = ?,
            resolution_reason = 'expired_below_floor'
        WHERE id = ?
        """,
        (to_utc_iso(now), existing.id),
    )
    conn.commit()
    return True


# ---------- readers ----------

def get_watching(
    conn: sqlite3.Connection,
    *,
    symbol: str,
) -> WatchlistEntry | None:
    """Return the active watch for ``symbol``, or ``None``."""
    row = conn.execute(
        """
        SELECT id, symbol, status, first_seen_at, last_seen_at,
               last_score, expires_at, promoted_signal_id,
               resolved_at, resolution_reason
        FROM watchlist
        WHERE symbol = ? AND status = 'watching'
        """,
        (symbol,),
    ).fetchone()
    return _row_to_entry(row) if row is not None else None


def list_watching(
    conn: sqlite3.Connection,
) -> list[WatchlistEntry]:
    """Return every active watch, oldest first."""
    rows = conn.execute(
        """
        SELECT id, symbol, status, first_seen_at, last_seen_at,
               last_score, expires_at, promoted_signal_id,
               resolved_at, resolution_reason
        FROM watchlist
        WHERE status = 'watching'
        ORDER BY first_seen_at ASC, id ASC
        """
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


# ---------- internals ----------

def _row_by_id(conn: sqlite3.Connection, row_id: int) -> WatchlistEntry | None:
    row = conn.execute(
        """
        SELECT id, symbol, status, first_seen_at, last_seen_at,
               last_score, expires_at, promoted_signal_id,
               resolved_at, resolution_reason
        FROM watchlist
        WHERE id = ?
        """,
        (row_id,),
    ).fetchone()
    return _row_to_entry(row) if row is not None else None


def _row_to_entry(row: sqlite3.Row) -> WatchlistEntry:
    return WatchlistEntry(
        id=int(row["id"]),
        symbol=str(row["symbol"]),
        status=str(row["status"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
        last_score=int(row["last_score"]),
        expires_at=str(row["expires_at"]),
        promoted_signal_id=(
            int(row["promoted_signal_id"])
            if row["promoted_signal_id"] is not None else None
        ),
        resolved_at=(str(row["resolved_at"]) if row["resolved_at"] else None),
        resolution_reason=(
            str(row["resolution_reason"]) if row["resolution_reason"] else None
        ),
    )
