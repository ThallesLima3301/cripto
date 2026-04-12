"""Tests for `crypto_monitor.signals.persistence` — dedup + insert.

Covers the rule-driven dedup contract from Block 6:

  * below_threshold (severity is None) -> not persisted
  * first signal for (symbol, candle_hour) -> inserted
  * duplicate (same severity, same candle_hour) -> skipped
  * lower severity than an existing row -> superseded (also skipped)
  * higher severity than an existing row -> escalation insert (both rows
    coexist in the table because the UNIQUE constraint is on the full
    (symbol, candle_hour, severity) tuple)
  * different candle_hours or different symbols do not dedup against
    each other
"""

from __future__ import annotations

from crypto_monitor.signals import insert_signal, load_candles
from crypto_monitor.signals.persistence import (
    REASON_BELOW_THRESHOLD,
    REASON_DUPLICATE,
    REASON_ESCALATED,
    REASON_INSERTED,
    REASON_SUPERSEDED,
)
from crypto_monitor.signals.types import SignalCandidate


def _mk_candidate(
    *,
    symbol: str = "BTCUSDT",
    candle_hour: str = "2026-04-11T14:00:00Z",
    severity: str | None = "normal",
    score: int = 70,
) -> SignalCandidate:
    """Build a minimal candidate. Only dedup-relevant fields are meaningful."""
    return SignalCandidate(
        symbol=symbol,
        candle_hour=candle_hour,
        detected_at="2026-04-11T14:05:00Z",
        price_at_signal=100.0,
        score=score,
        severity=severity,
        drop_1h_pct=None,
        drop_24h_pct=None,
        drop_7d_pct=None,
        drop_30d_pct=None,
        drop_180d_pct=None,
        dominant_trigger_timeframe=None,
        trigger_reason="test",
        drop_trigger_pct=None,
        recent_30d_high=None,
        recent_180d_high=None,
        distance_from_30d_high_pct=None,
        distance_from_180d_high_pct=None,
        rsi_1h=None,
        rsi_4h=None,
        rel_volume=None,
        dist_support_pct=None,
        support_level_price=None,
        reversal_signal=False,
        reversal_pattern=None,
        trend_context_4h="sideways",
        trend_context_1d="sideways",
        score_breakdown={"total": score},
    )


def _count_signals(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]


# ---------- below-threshold guard ----------

def test_below_threshold_candidate_is_not_inserted(memory_db):
    candidate = _mk_candidate(severity=None, score=30)
    result = insert_signal(memory_db, candidate)

    assert result.inserted is False
    assert result.reason == REASON_BELOW_THRESHOLD
    assert result.signal_id is None
    assert _count_signals(memory_db) == 0


# ---------- first insert ----------

def test_first_signal_for_candle_hour_is_inserted(memory_db):
    candidate = _mk_candidate(severity="normal")
    result = insert_signal(memory_db, candidate)

    assert result.inserted is True
    assert result.reason == REASON_INSERTED
    assert result.signal_id is not None
    assert _count_signals(memory_db) == 1


def test_first_insert_persists_first_class_fields(memory_db):
    # Sanity: the candidate's fields actually land in the right columns.
    candidate = _mk_candidate(severity="strong", score=72)
    result = insert_signal(memory_db, candidate)

    row = memory_db.execute(
        "SELECT symbol, candle_hour, severity, score, trigger_reason, alerted "
        "FROM signals WHERE id = ?",
        (result.signal_id,),
    ).fetchone()
    assert row["symbol"] == "BTCUSDT"
    assert row["candle_hour"] == "2026-04-11T14:00:00Z"
    assert row["severity"] == "strong"
    assert row["score"] == 72
    assert row["trigger_reason"] == "test"
    # alerted should start at 0 — Block 7 is responsible for flipping it.
    assert row["alerted"] == 0


# ---------- dedup within the same candle_hour ----------

def test_duplicate_same_severity_is_skipped(memory_db):
    first = _mk_candidate(severity="normal")
    second = _mk_candidate(severity="normal")

    r1 = insert_signal(memory_db, first)
    r2 = insert_signal(memory_db, second)

    assert r1.inserted is True
    assert r2.inserted is False
    assert r2.reason == REASON_DUPLICATE
    assert _count_signals(memory_db) == 1


def test_lower_severity_than_existing_is_superseded(memory_db):
    # A strong signal already exists for this (symbol, candle_hour).
    # A later re-evaluation that scores only "normal" must not downgrade
    # the record — the existing stronger row wins.
    strong = _mk_candidate(severity="strong", score=75)
    later_normal = _mk_candidate(severity="normal", score=60)

    r1 = insert_signal(memory_db, strong)
    r2 = insert_signal(memory_db, later_normal)

    assert r1.inserted is True
    assert r2.inserted is False
    assert r2.reason == REASON_SUPERSEDED
    assert _count_signals(memory_db) == 1


def test_very_strong_supersedes_later_strong_attempt(memory_db):
    # Same logic one tier up — very_strong exists, strong should be skipped.
    very_strong = _mk_candidate(severity="very_strong", score=85)
    later_strong = _mk_candidate(severity="strong", score=70)

    insert_signal(memory_db, very_strong)
    r = insert_signal(memory_db, later_strong)

    assert r.inserted is False
    assert r.reason == REASON_SUPERSEDED
    assert _count_signals(memory_db) == 1


# ---------- severity escalation exception ----------

def test_strong_escalation_over_existing_normal(memory_db):
    normal = _mk_candidate(severity="normal", score=55)
    strong = _mk_candidate(severity="strong", score=72)

    r1 = insert_signal(memory_db, normal)
    r2 = insert_signal(memory_db, strong)

    assert r1.inserted is True
    assert r1.reason == REASON_INSERTED
    assert r2.inserted is True
    assert r2.reason == REASON_ESCALATED
    # Both rows coexist because the UNIQUE is (symbol, candle_hour, severity).
    assert _count_signals(memory_db) == 2


def test_very_strong_escalation_over_existing_strong(memory_db):
    strong = _mk_candidate(severity="strong", score=70)
    very_strong = _mk_candidate(severity="very_strong", score=85)

    insert_signal(memory_db, strong)
    r = insert_signal(memory_db, very_strong)

    assert r.inserted is True
    assert r.reason == REASON_ESCALATED
    assert _count_signals(memory_db) == 2


def test_escalation_chain_normal_then_strong_then_very_strong(memory_db):
    # The classic mid-candle escalation sequence: three successive scans
    # each score higher than the last. All three should end up in the
    # table as separate rows.
    insert_signal(memory_db, _mk_candidate(severity="normal", score=55))
    r2 = insert_signal(memory_db, _mk_candidate(severity="strong", score=70))
    r3 = insert_signal(memory_db, _mk_candidate(severity="very_strong", score=85))

    assert r2.reason == REASON_ESCALATED
    assert r3.reason == REASON_ESCALATED
    assert _count_signals(memory_db) == 3


# ---------- different keys are independent ----------

def test_different_candle_hour_is_not_dedup_scope(memory_db):
    first_hour = _mk_candidate(
        candle_hour="2026-04-11T14:00:00Z", severity="normal"
    )
    second_hour = _mk_candidate(
        candle_hour="2026-04-11T15:00:00Z", severity="normal"
    )

    r1 = insert_signal(memory_db, first_hour)
    r2 = insert_signal(memory_db, second_hour)

    assert r1.inserted is True
    assert r2.inserted is True
    # The second is a plain INSERT, not an escalation — different candle_hour.
    assert r2.reason == REASON_INSERTED
    assert _count_signals(memory_db) == 2


def test_different_symbols_do_not_dedup_against_each_other(memory_db):
    btc = _mk_candidate(symbol="BTCUSDT", severity="normal")
    eth = _mk_candidate(symbol="ETHUSDT", severity="normal")

    r1 = insert_signal(memory_db, btc)
    r2 = insert_signal(memory_db, eth)

    assert r1.inserted is True
    assert r2.inserted is True
    assert _count_signals(memory_db) == 2


# ---------- load_candles helper ----------

def test_load_candles_returns_chronological_order(memory_db):
    # Insert three candles out of order — the helper must return them
    # oldest-first so indicator code can rely on `candles[-1]` being
    # the most recent.
    rows = [
        ("2026-04-11T14:00:00Z", 100.0, 101.0, 99.0, 100.5,
         "2026-04-11T15:00:00Z"),
        ("2026-04-11T13:00:00Z",  99.0, 100.0, 98.0,  99.5,
         "2026-04-11T14:00:00Z"),
        ("2026-04-11T15:00:00Z", 100.5, 102.0, 100.0, 101.5,
         "2026-04-11T16:00:00Z"),
    ]
    for open_time, o, h, l, c, close_time in rows:
        memory_db.execute(
            """
            INSERT INTO candles
                (symbol, interval, open_time, open, high, low, close,
                 volume, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("BTCUSDT", "1h", open_time, o, h, l, c, 50.0, close_time),
        )
    memory_db.commit()

    loaded = load_candles(memory_db, "BTCUSDT", "1h")
    assert [c.open_time for c in loaded] == [
        "2026-04-11T13:00:00Z",
        "2026-04-11T14:00:00Z",
        "2026-04-11T15:00:00Z",
    ]


def test_load_candles_honors_limit(memory_db):
    # 5 candles in, but limit=3 should keep only the 3 most recent —
    # still returned chronologically.
    for i in range(5):
        memory_db.execute(
            """
            INSERT INTO candles
                (symbol, interval, open_time, open, high, low, close,
                 volume, close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "BTCUSDT", "1h",
                f"2026-04-11T{10 + i:02d}:00:00Z",
                100.0, 100.0, 100.0, 100.0, 50.0,
                f"2026-04-11T{11 + i:02d}:00:00Z",
            ),
        )
    memory_db.commit()

    loaded = load_candles(memory_db, "BTCUSDT", "1h", limit=3)
    assert [c.open_time for c in loaded] == [
        "2026-04-11T12:00:00Z",
        "2026-04-11T13:00:00Z",
        "2026-04-11T14:00:00Z",
    ]
