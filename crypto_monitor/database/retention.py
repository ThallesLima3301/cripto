"""Candle-table retention.

The `candles` table grows every scan; without pruning it would reach
gigabytes over a year of continuous 1h ingestion on dozens of
symbols. This module caps the table to a configurable number of
most-recent candles per (symbol, interval) pair.

Only `candles` is pruned — the rest of the tables (signals, buys,
notifications, weekly summaries) hold data the user actively wants
to keep as history and are small anyway.

Design:
  * Retention is driven entirely by `RetentionSettings.max_candles_*`.
    Unknown intervals are passed through untouched so this module is
    safe even if the config contains extra intervals that don't have
    dedicated caps.
  * Pruning is per (symbol, interval): we delete every row whose id
    is not in the top-N newest `open_time`s. Using id makes the
    delete independent of ISO string ordering pitfalls.
  * `VACUUM` is NOT run here — the maintenance entrypoint decides
    whether to VACUUM based on its own config flag.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from crypto_monitor.config.settings import RetentionSettings


logger = logging.getLogger(__name__)


# Maps interval string → the RetentionSettings field that caps it.
_CAP_BY_INTERVAL: dict[str, str] = {
    "1h": "max_candles_1h",
    "4h": "max_candles_4h",
    "1d": "max_candles_1d",
}


@dataclass(frozen=True)
class PruneReport:
    """Summary of a `prune_old_candles` run.

    `per_interval` is the count of rows deleted grouped by interval
    (summed across every symbol). `total_deleted` is the sum of those
    counts.
    """
    per_interval: dict[str, int]
    total_deleted: int


def prune_old_candles(
    conn: sqlite3.Connection,
    retention: RetentionSettings,
) -> PruneReport:
    """Delete candles older than the configured cap per (symbol, interval).

    Walks every (symbol, interval) pair currently present in the
    table, keeps the newest N rows (where N comes from
    `_CAP_BY_INTERVAL`), and deletes the rest. Intervals with no
    configured cap are left untouched.

    Commits at the end. Safe to run concurrently with an ongoing
    scan — the busy_timeout PRAGMA handles the brief overlap.
    """
    per_interval: dict[str, int] = {}
    total = 0

    pairs = conn.execute(
        """
        SELECT symbol, interval FROM candles
        GROUP BY symbol, interval
        """
    ).fetchall()

    for row in pairs:
        symbol = row["symbol"]
        interval = row["interval"]
        field = _CAP_BY_INTERVAL.get(interval)
        if field is None:
            continue
        cap = int(getattr(retention, field))
        if cap <= 0:
            continue

        cur = conn.execute(
            """
            DELETE FROM candles
            WHERE symbol = ? AND interval = ?
              AND id NOT IN (
                SELECT id FROM candles
                WHERE symbol = ? AND interval = ?
                ORDER BY open_time DESC
                LIMIT ?
              )
            """,
            (symbol, interval, symbol, interval, cap),
        )
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted:
            per_interval[interval] = per_interval.get(interval, 0) + deleted
            total += deleted
            logger.info(
                "retention pruned %s %s: %d row(s)", symbol, interval, deleted
            )

    conn.commit()
    return PruneReport(per_interval=per_interval, total_deleted=total)


def vacuum(conn: sqlite3.Connection) -> None:
    """Run SQLite VACUUM.

    Called by the maintenance entrypoint only when
    `RetentionSettings.vacuum_on_maintenance` is True. VACUUM
    rewrites the entire database file and is therefore expensive; we
    leave the scheduling decision to the caller.
    """
    conn.execute("VACUUM")
