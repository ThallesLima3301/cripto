"""Manual buy insertion + read helpers.

Design goals
------------
* The insert function is the only way to add a buy row. It owns
  quantity derivation (`amount_invested / price`), the `created_at`
  timestamp, and the optional signal link.

* The signal link is validated: if a `signal_id` is provided, we
  fail loudly when that row does not exist in `signals`. A silent
  orphan link would be worse than a KeyError — the user needs to
  know they typed the wrong ID.

* No portfolio math here. `BuyRecord` stores what the user told us
  plus a derived `quantity`; percent returns, verdicts, and the
  hourly-low stats all live in `evaluation.buy_eval`.

* Input timestamps must be timezone-aware. We store UTC ISO strings
  end-to-end (same contract as the rest of the project), so we
  convert at the boundary and never let naive datetimes leak into
  the DB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


@dataclass(frozen=True)
class BuyRecord:
    """A row from the `buys` table.

    Mirrors the schema 1:1 apart from ordering. `quantity` is
    derived at insert time from `amount_invested / price` so
    downstream consumers never need to re-derive it.
    """
    id: int
    symbol: str
    bought_at: str            # UTC ISO
    price: float
    amount_invested: float
    quote_currency: str
    quantity: float
    signal_id: int | None
    note: str | None
    created_at: str           # UTC ISO


def insert_buy(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    bought_at: datetime,
    price: float,
    amount_invested: float,
    quote_currency: str = "USDT",
    quantity: float | None = None,
    signal_id: int | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> BuyRecord:
    """Record a manual buy.

    Arguments
    ---------
    symbol          — Binance pair, e.g. "BTCUSDT".
    bought_at       — TZ-aware datetime of the purchase.
    price           — Execution price per unit (quote currency).
    amount_invested — Quote-currency amount spent; when `quantity`
                      is omitted it is derived as
                      `amount_invested / price`.
    quote_currency  — The quote asset, default "USDT".
    quantity        — Optional override for the executed base-asset
                      amount. Pass this when the user knows the
                      exact filled quantity (e.g. from a Binance
                      trade report) and wants the ledger to match
                      the exchange rather than the derived value.
                      Must be > 0 when provided.
    signal_id       — Optional link to a row in `signals`. Validated
                      at insert time; raises ValueError if the row
                      does not exist.
    note            — Free-form user note.
    now             — Override for `created_at`; only used by tests
                      so they can pin a deterministic timestamp.
    """
    if price <= 0:
        raise ValueError("price must be > 0")
    if amount_invested <= 0:
        raise ValueError("amount_invested must be > 0")
    if quantity is not None and quantity <= 0:
        raise ValueError("quantity must be > 0 when provided")
    if bought_at.tzinfo is None:
        raise ValueError("bought_at must be timezone-aware")

    if signal_id is not None and not _signal_exists(conn, signal_id):
        raise ValueError(f"signal_id {signal_id} does not exist in signals")

    created_at = now if now is not None else now_utc()
    if created_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    if quantity is None:
        quantity = amount_invested / price

    bought_at_iso = to_utc_iso(bought_at)
    created_at_iso = to_utc_iso(created_at)

    cur = conn.execute(
        """
        INSERT INTO buys (
            symbol, bought_at, price, amount_invested,
            quote_currency, quantity, signal_id, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            bought_at_iso,
            float(price),
            float(amount_invested),
            quote_currency,
            float(quantity),
            signal_id,
            note,
            created_at_iso,
        ),
    )
    conn.commit()

    return BuyRecord(
        id=int(cur.lastrowid),
        symbol=symbol,
        bought_at=bought_at_iso,
        price=float(price),
        amount_invested=float(amount_invested),
        quote_currency=quote_currency,
        quantity=float(quantity),
        signal_id=signal_id,
        note=note,
        created_at=created_at_iso,
    )


def get_buy(conn: sqlite3.Connection, buy_id: int) -> BuyRecord | None:
    """Fetch a single buy row by id, or None if missing."""
    row = conn.execute(
        """
        SELECT id, symbol, bought_at, price, amount_invested,
               quote_currency, quantity, signal_id, note, created_at
        FROM buys WHERE id = ?
        """,
        (buy_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def list_buys(
    conn: sqlite3.Connection, *, symbol: str | None = None
) -> list[BuyRecord]:
    """Return buys in chronological order, optionally filtered by symbol."""
    if symbol is None:
        rows = conn.execute(
            """
            SELECT id, symbol, bought_at, price, amount_invested,
                   quote_currency, quantity, signal_id, note, created_at
            FROM buys
            ORDER BY bought_at ASC, id ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, symbol, bought_at, price, amount_invested,
                   quote_currency, quantity, signal_id, note, created_at
            FROM buys
            WHERE symbol = ?
            ORDER BY bought_at ASC, id ASC
            """,
            (symbol,),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


# ---------- internals ----------

def _signal_exists(conn: sqlite3.Connection, signal_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM signals WHERE id = ?", (signal_id,)
    ).fetchone()
    return row is not None


def _row_to_record(row: sqlite3.Row) -> BuyRecord:
    return BuyRecord(
        id=int(row["id"]),
        symbol=row["symbol"],
        bought_at=row["bought_at"],
        price=float(row["price"]),
        amount_invested=float(row["amount_invested"]),
        quote_currency=row["quote_currency"],
        quantity=float(row["quantity"]),
        signal_id=(int(row["signal_id"]) if row["signal_id"] is not None else None),
        note=row["note"],
        created_at=row["created_at"],
    )
