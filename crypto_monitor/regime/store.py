"""Persistence for regime snapshots."""

from __future__ import annotations

import sqlite3

from crypto_monitor.regime.types import RegimeSnapshot


def save_regime_snapshot(
    conn: sqlite3.Connection,
    snapshot: RegimeSnapshot,
) -> int:
    """INSERT a regime snapshot and return the row id."""
    cur = conn.execute(
        """
        INSERT INTO regime_snapshots
            (label, btc_ema_short, btc_ema_long, btc_atr_14d,
             atr_percentile, determined_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.label,
            snapshot.btc_ema_short,
            snapshot.btc_ema_long,
            snapshot.btc_atr_14d,
            snapshot.atr_percentile,
            snapshot.determined_at,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def load_latest_regime(
    conn: sqlite3.Connection,
) -> RegimeSnapshot | None:
    """Return the most recent regime snapshot, or ``None``."""
    row = conn.execute(
        """
        SELECT label, btc_ema_short, btc_ema_long, btc_atr_14d,
               atr_percentile, determined_at
        FROM regime_snapshots
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return RegimeSnapshot(
        label=row["label"],
        btc_ema_short=row["btc_ema_short"],
        btc_ema_long=row["btc_ema_long"],
        btc_atr_14d=row["btc_atr_14d"],
        atr_percentile=row["atr_percentile"],
        determined_at=row["determined_at"],
    )


def list_regime_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[RegimeSnapshot]:
    """Return recent regime snapshots, newest first.

    Used by the dashboard ``/api/regime/history`` endpoint to render
    the regime timeline. Bounded by ``limit`` so the response stays
    small even after months of scans (the table grows by one row per
    scan when the feature is enabled).
    """
    if limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT label, btc_ema_short, btc_ema_long, btc_atr_14d,
               atr_percentile, determined_at
        FROM regime_snapshots
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [
        RegimeSnapshot(
            label=r["label"],
            btc_ema_short=r["btc_ema_short"],
            btc_ema_long=r["btc_ema_long"],
            btc_atr_14d=r["btc_atr_14d"],
            atr_percentile=r["atr_percentile"],
            determined_at=r["determined_at"],
        )
        for r in rows
    ]
