"""Persistence helpers for the sell subsystem.

Block 19 boundary: every helper here is pure SQL on an open
connection. Nothing decides whether to sell — these functions only
read and write rows the future engine will rely on.

All timestamps round-trip as UTC ISO 8601 strings. Callers that have
``datetime`` objects must convert at the boundary; the helpers refuse
naive datetimes via the small ``_to_iso`` shim below.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from crypto_monitor.buys.manual import BuyRecord, _row_to_record
from crypto_monitor.sell.types import SellSignal
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


# ---------- high-water-mark table ----------

def upsert_high_watermark(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    buy_id: int,
    high_watermark: float,
    now: datetime | None = None,
) -> None:
    """INSERT-or-UPDATE the post-entry high observed for ``buy_id``.

    The row is keyed by (symbol, buy_id) so trailing-stop tracking is
    per-position, not per-symbol — two open buys on the same coin keep
    independent watermarks.

    Update semantics: the stored value monotonically rises. A call with
    a value at or below the existing watermark is a no-op for the
    number itself (the timestamp still bumps so callers can tell the
    row was touched this scan).
    """
    if high_watermark <= 0:
        raise ValueError("high_watermark must be > 0")

    ts = _to_iso(now if now is not None else now_utc())
    conn.execute(
        """
        INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol, buy_id) DO UPDATE SET
            high_watermark = MAX(sell_tracking.high_watermark, excluded.high_watermark),
            updated_at = excluded.updated_at
        """,
        (symbol, buy_id, float(high_watermark), ts),
    )
    conn.commit()


def get_high_watermark(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    buy_id: int,
) -> float | None:
    """Return the recorded high-water mark, or ``None`` if not tracked yet."""
    row = conn.execute(
        """
        SELECT high_watermark FROM sell_tracking
        WHERE symbol = ? AND buy_id = ?
        """,
        (symbol, buy_id),
    ).fetchone()
    if row is None:
        return None
    return float(row["high_watermark"])


# ---------- sell signals (append-only log) ----------

def insert_sell_signal(
    conn: sqlite3.Connection,
    signal: SellSignal,
) -> int:
    """Insert a sell signal row, return the new ``id``.

    The ``id`` and ``alerted`` fields on the input dataclass are
    ignored — ``id`` is assigned by SQLite, ``alerted`` defaults to 0
    (the alert pipeline owns the toggle later).
    """
    if signal.price_at_signal <= 0:
        raise ValueError("price_at_signal must be > 0")
    if not signal.rule_triggered:
        raise ValueError("rule_triggered must be a non-empty string")

    cur = conn.execute(
        """
        INSERT INTO sell_signals (
            symbol, buy_id, detected_at, price_at_signal,
            rule_triggered, severity, reason, pnl_pct,
            regime_at_signal, alerted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal.symbol,
            int(signal.buy_id),
            signal.detected_at,
            float(signal.price_at_signal),
            signal.rule_triggered,
            signal.severity,
            signal.reason,
            (float(signal.pnl_pct) if signal.pnl_pct is not None else None),
            signal.regime_at_signal,
            int(signal.alerted),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def last_sell_signal_time(
    conn: sqlite3.Connection,
    *,
    buy_id: int,
    rule_triggered: str | None = None,
) -> str | None:
    """Return the most recent ``detected_at`` for ``buy_id``, or ``None``.

    When ``rule_triggered`` is provided the lookup is filtered to that
    specific rule so the caller can enforce a per-(buy, rule) cooldown
    (e.g. stop-loss firing once should not silence a later take-profit
    on the same position). When ``rule_triggered`` is ``None`` the
    query is buy-level, matching the Block 19 behavior.
    """
    if rule_triggered is None:
        row = conn.execute(
            """
            SELECT detected_at FROM sell_signals
            WHERE buy_id = ?
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (buy_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT detected_at FROM sell_signals
            WHERE buy_id = ? AND rule_triggered = ?
            ORDER BY detected_at DESC, id DESC
            LIMIT 1
            """,
            (buy_id, rule_triggered),
        ).fetchone()
    if row is None:
        return None
    return str(row["detected_at"])


# ---------- buys close-out ----------

def load_open_buys(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
) -> list[BuyRecord]:
    """Return buys that have NOT been marked sold, oldest first.

    ``symbol`` filters the result to a single coin when provided.
    Sold rows (``sold_at IS NOT NULL``) are excluded so the future sell
    engine can iterate exactly the positions that still need watching.
    """
    base = """
        SELECT id, symbol, bought_at, price, amount_invested,
               quote_currency, quantity, signal_id, note, created_at
        FROM buys
        WHERE sold_at IS NULL
    """
    if symbol is None:
        rows = conn.execute(
            base + " ORDER BY bought_at ASC, id ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            base + " AND symbol = ? ORDER BY bought_at ASC, id ASC",
            (symbol,),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


def record_sale(
    conn: sqlite3.Connection,
    *,
    buy_id: int,
    sold_at: datetime,
    sold_price: float,
    sold_note: str | None = None,
) -> None:
    """Mark a buy as sold by stamping the three close-out columns.

    Validation rules (raised as ``ValueError`` so callers can surface a
    user-facing message):

      * the buy must exist and must not already have ``sold_at`` set
        (no double-sell — re-marking would silently overwrite the
        original close-out timestamp);
      * ``sold_price`` must be positive (a zero or negative fill price
        is meaningless and almost certainly a typo);
      * ``sold_at`` must be at or after ``bought_at`` (selling before
        you bought is a data-entry mistake).

    The companion ``sell_tracking`` row is intentionally left untouched
    — the watermark history is informational and a future cleanup pass
    can prune closed-position rows separately.
    """
    if sold_price <= 0:
        raise ValueError("sold_price must be > 0")
    if sold_at.tzinfo is None:
        raise ValueError("sold_at must be timezone-aware")

    row = conn.execute(
        "SELECT bought_at, sold_at FROM buys WHERE id = ?",
        (buy_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"buy_id {buy_id} does not exist")
    if row["sold_at"] is not None:
        raise ValueError(f"buy_id {buy_id} is already marked sold")

    sold_at_iso = _to_iso(sold_at)
    if sold_at_iso < str(row["bought_at"]):
        raise ValueError(
            f"sold_at ({sold_at_iso}) is earlier than bought_at "
            f"({row['bought_at']})"
        )

    conn.execute(
        """
        UPDATE buys
        SET sold_at = ?, sold_price = ?, sold_note = ?
        WHERE id = ?
        """,
        (sold_at_iso, float(sold_price), sold_note, buy_id),
    )
    conn.commit()


# ---------- internals ----------

def _to_iso(value: datetime) -> str:
    """Convert a tz-aware datetime to the canonical UTC ISO string."""
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return to_utc_iso(value)
