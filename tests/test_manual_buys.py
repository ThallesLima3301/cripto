"""Tests for `crypto_monitor.buys.manual`.

Covers:
  * basic insertion + derived quantity
  * defaulting and validation of edge inputs
  * optional signal linking (happy path + unknown-id rejection)
  * `get_buy` + `list_buys` round-trip
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crypto_monitor.buys import get_buy, insert_buy, list_buys


UTC = timezone.utc


def _insert_signal_row(conn, *, symbol="BTCUSDT", severity="strong", score=72) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown
        ) VALUES (?, ?, ?, ?, ?, ?, 'test', 0, '{}')
        """,
        (
            symbol,
            "2026-03-12T14:05:00Z",
            "2026-03-12T14:00:00Z",
            40.0,
            score,
            severity,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------- basic insert ----------

def test_insert_buy_writes_row_with_derived_quantity(memory_db):
    record = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        price=50.0,
        amount_invested=100.0,
        now=datetime(2026, 4, 1, 12, 5, tzinfo=UTC),
    )
    assert record.id > 0
    assert record.symbol == "BTCUSDT"
    assert record.price == 50.0
    assert record.amount_invested == 100.0
    # quantity is derived from amount_invested / price.
    assert record.quantity == pytest.approx(2.0)
    assert record.quote_currency == "USDT"
    assert record.signal_id is None
    assert record.bought_at == "2026-04-01T12:00:00Z"
    assert record.created_at == "2026-04-01T12:05:00Z"

    # Round-trip via get_buy.
    fetched = get_buy(memory_db, record.id)
    assert fetched is not None
    assert fetched == record


def test_insert_buy_without_now_uses_wall_clock(memory_db):
    # `now` is optional — the insert uses `now_utc()` when omitted.
    record = insert_buy(
        memory_db,
        symbol="ETHUSDT",
        bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        price=2000.0,
        amount_invested=1000.0,
    )
    # We don't assert the exact value; just that it was populated
    # and parses as a UTC ISO string.
    assert record.created_at.endswith("Z")
    assert len(record.created_at) == len("2026-04-01T12:05:00Z")


# ---------- validation ----------

def test_insert_buy_rejects_non_positive_price(memory_db):
    with pytest.raises(ValueError, match="price"):
        insert_buy(
            memory_db,
            symbol="BTCUSDT",
            bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            price=0.0,
            amount_invested=100.0,
        )


def test_insert_buy_rejects_non_positive_amount(memory_db):
    with pytest.raises(ValueError, match="amount_invested"):
        insert_buy(
            memory_db,
            symbol="BTCUSDT",
            bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            price=50.0,
            amount_invested=0.0,
        )


def test_insert_buy_rejects_naive_bought_at(memory_db):
    with pytest.raises(ValueError, match="timezone-aware"):
        insert_buy(
            memory_db,
            symbol="BTCUSDT",
            bought_at=datetime(2026, 4, 1, 12, 0),  # naive
            price=50.0,
            amount_invested=100.0,
        )


# ---------- optional signal linking ----------

def test_insert_buy_links_to_existing_signal(memory_db):
    signal_id = _insert_signal_row(memory_db)

    record = insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        price=42.0,
        amount_invested=210.0,
        signal_id=signal_id,
        note="bought into the STRONG crash signal",
        now=datetime(2026, 4, 1, 12, 5, tzinfo=UTC),
    )
    assert record.signal_id == signal_id
    assert record.note == "bought into the STRONG crash signal"

    # Verify the FK landed in the DB.
    row = memory_db.execute(
        "SELECT signal_id FROM buys WHERE id = ?", (record.id,)
    ).fetchone()
    assert row["signal_id"] == signal_id


def test_insert_buy_rejects_unknown_signal_id(memory_db):
    with pytest.raises(ValueError, match="signal_id"):
        insert_buy(
            memory_db,
            symbol="BTCUSDT",
            bought_at=datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
            price=42.0,
            amount_invested=210.0,
            signal_id=9999,  # does not exist
        )


# ---------- list_buys ----------

def test_list_buys_chronological_and_filterable(memory_db):
    t0 = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    t1 = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 3, 12, 0, tzinfo=UTC)

    insert_buy(
        memory_db, symbol="BTCUSDT", bought_at=t1,
        price=50.0, amount_invested=100.0, now=t1,
    )
    insert_buy(
        memory_db, symbol="ETHUSDT", bought_at=t0,
        price=2000.0, amount_invested=1000.0, now=t0,
    )
    insert_buy(
        memory_db, symbol="BTCUSDT", bought_at=t2,
        price=60.0, amount_invested=120.0, now=t2,
    )

    # Unfiltered: three buys in chronological order.
    all_buys = list_buys(memory_db)
    assert [b.bought_at for b in all_buys] == [
        "2026-04-01T12:00:00Z",
        "2026-04-02T12:00:00Z",
        "2026-04-03T12:00:00Z",
    ]

    # Symbol-filtered.
    btc_only = list_buys(memory_db, symbol="BTCUSDT")
    assert len(btc_only) == 2
    assert all(b.symbol == "BTCUSDT" for b in btc_only)
