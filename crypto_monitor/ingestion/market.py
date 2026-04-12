"""Market data ingestion.

Incrementally fetches new closed candles from Binance for every active
symbol × interval pair and persists them to the `candles` table.

Cold-start vs. incremental decision is driven entirely by the
`processing_state` table:

  * No row → cold start. Fetch the most recent `bootstrap_limit` candles
    so we have enough history for RSI, EMA, and 30d/180d context (250 1d
    candles cover ~8 months on the slowest interval).
  * Row exists → incremental. Fetch from `last_open + 1ms` onward,
    capped at Binance's max limit (1000) per call. The scan loop runs
    every 5 minutes, so this stays well under 1 round trip per pair.

Idempotency: candles are inserted via `INSERT OR IGNORE`, leveraging
the `UNIQUE(symbol, interval, open_time)` constraint from Block 2.
Re-running the same scan does not create duplicates.

Errors are isolated per (symbol, interval): one bad pair does not abort
the rest of the scan. Failures are logged and recorded in the report.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from crypto_monitor.binance.client import BinanceClient, Kline
from crypto_monitor.utils.time_utils import (
    ms_to_utc_iso,
    now_utc,
    to_utc_iso,
    utc_iso_to_ms,
)


logger = logging.getLogger(__name__)


# Cap on incremental fetches per request. Binance max is 1000.
_INCREMENTAL_LIMIT = 1000


@dataclass
class IngestReport:
    """Summary of one `ingest_all_symbols` run."""

    per_symbol: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    total_new: int = 0

    def summary_line(self) -> str:
        parts = [f"{sym}={sum(iv.values())}" for sym, iv in self.per_symbol.items()]
        err = f" errors={len(self.errors)}" if self.errors else ""
        return f"ingest total={self.total_new} " + " ".join(parts) + err


def ingest_all_symbols(
    conn: sqlite3.Connection,
    client: BinanceClient,
    symbols: list[str],
    intervals: list[str],
    bootstrap_limit: int = 250,
) -> IngestReport:
    """Ingest closed candles for every (symbol, interval) pair.

    Always returns a report — exceptions are caught per pair and recorded
    so the calling scheduler can log them and continue with scoring.
    """
    report = IngestReport()
    now_ms = int(now_utc().timestamp() * 1000)

    for symbol in symbols:
        report.per_symbol.setdefault(symbol, {})
        for interval in intervals:
            try:
                count = _ingest_one(
                    conn, client, symbol, interval, bootstrap_limit, now_ms
                )
                report.per_symbol[symbol][interval] = count
                report.total_new += count
                logger.info(
                    "ingested %s %s: %d new candle(s)", symbol, interval, count
                )
            except Exception as exc:  # noqa: BLE001 — we want to keep scanning
                logger.exception("ingest error %s %s", symbol, interval)
                report.errors.append(f"{symbol} {interval}: {exc}")
                report.per_symbol[symbol][interval] = 0

    return report


# ---------- per-pair worker ----------

def _ingest_one(
    conn: sqlite3.Connection,
    client: BinanceClient,
    symbol: str,
    interval: str,
    bootstrap_limit: int,
    now_ms: int,
) -> int:
    state_key = _state_key(symbol, interval)
    last_open_iso = _get_state(conn, state_key)

    if last_open_iso is None:
        # Cold start.
        klines = client.get_klines(
            symbol, interval, limit=bootstrap_limit, now_ms=now_ms
        )
    else:
        # Incremental: ask for everything strictly after the last stored open.
        start_ms = utc_iso_to_ms(last_open_iso) + 1
        if start_ms >= now_ms:
            return 0
        klines = client.get_klines(
            symbol,
            interval,
            limit=_INCREMENTAL_LIMIT,
            start_time_ms=start_ms,
            now_ms=now_ms,
        )

    if not klines:
        return 0

    inserted = _persist_klines(conn, klines)

    # Always advance processing_state to the highest open_time we just saw,
    # even if INSERT OR IGNORE skipped some rows (we still made progress).
    max_open_ms = max(k.open_time_ms for k in klines)
    _set_state(conn, state_key, ms_to_utc_iso(max_open_ms))
    conn.commit()
    return inserted


def _persist_klines(conn: sqlite3.Connection, klines: list[Kline]) -> int:
    inserted = 0
    for k in klines:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO candles
                (symbol, interval, open_time, open, high, low, close,
                 volume, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                k.symbol,
                k.interval,
                ms_to_utc_iso(k.open_time_ms),
                k.open,
                k.high,
                k.low,
                k.close,
                k.volume,
                ms_to_utc_iso(k.close_time_ms),
            ),
        )
        if cur.rowcount > 0:
            inserted += cur.rowcount
    return inserted


# ---------- processing_state helpers ----------

def _state_key(symbol: str, interval: str) -> str:
    return f"ingest_last_open:{symbol}:{interval}"


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM processing_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    ts = to_utc_iso(now_utc())
    conn.execute(
        """
        INSERT INTO processing_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (key, value, ts),
    )
