"""Tests for `crypto_monitor.reports.weekly`.

Covers:
  * pure generation on an empty DB → zero counts, quiet-week body
  * signal counting + severity breakdown restricted to the 7d window
  * top-drop selection (biggest drop_trigger_pct, deterministic tie break)
  * buy counting restricted to the window
  * matured-verdict aggregation across signal_evaluations + buy_evaluations
  * body rendering contains the key facts
  * persist writes a row with schema-visible fields and sent=0
  * send_weekly_summary marks sent=1 on success and leaves it 0 on failure
  * generate_and_send_weekly_summary end-to-end orchestration
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from crypto_monitor.buys import insert_buy
from crypto_monitor.notifications.ntfy import (
    REASON_NETWORK_ERROR,
    REASON_SENT,
    SendResult,
)
from crypto_monitor.reports.weekly import (
    WeeklySummary,
    generate_and_send_weekly_summary,
    generate_weekly_summary,
    persist_weekly_summary,
    send_weekly_summary,
)


UTC = timezone.utc


# ---------- stubs / helpers ----------

@dataclass
class _SenderCall:
    title: str
    body: str
    priority: str
    tags: tuple[str, ...]


class _RecordingSender:
    """Stub ntfy sender.

    Captures every call and returns a pre-canned result. Lets tests
    assert on what the service layer tried to push without touching
    real HTTP.
    """

    def __init__(self, result: SendResult) -> None:
        self._result = result
        self.calls: list[_SenderCall] = []

    def __call__(
        self,
        ntfy: Any,
        title: str,
        body: str,
        *,
        priority: str = "default",
        tags: tuple[str, ...] = (),
        **_: Any,
    ) -> SendResult:
        self.calls.append(
            _SenderCall(title=title, body=body, priority=priority, tags=tags)
        )
        return self._result


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_signal(
    conn,
    *,
    symbol: str,
    detected_at: datetime,
    severity: str = "strong",
    score: int = 72,
    drop_trigger_pct: float | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown, drop_trigger_pct
        ) VALUES (?, ?, ?, ?, ?, ?, 'test', 0, '{}', ?)
        """,
        (
            symbol,
            _iso(detected_at),
            _iso(detected_at),
            100.0,
            score,
            severity,
            drop_trigger_pct,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_signal_evaluation(
    conn,
    *,
    signal_id: int,
    evaluated_at: datetime,
    verdict: str,
) -> None:
    conn.execute(
        """
        INSERT INTO signal_evaluations (
            signal_id, evaluated_at, price_at_signal,
            price_24h_later, price_7d_later, price_30d_later,
            return_24h_pct, return_7d_pct, return_30d_pct,
            max_gain_7d_pct, max_loss_7d_pct, verdict
        ) VALUES (?, ?, 100.0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)
        """,
        (signal_id, _iso(evaluated_at), verdict),
    )
    conn.commit()


def _insert_buy_evaluation(
    conn,
    *,
    buy_id: int,
    evaluated_at: datetime,
    verdict: str,
) -> None:
    conn.execute(
        """
        INSERT INTO buy_evaluations (
            buy_id, evaluated_at,
            day_open, day_low_hourly, day_low_hourly_time,
            pct_from_day_open_to_low_hourly, pct_from_buy_to_low_hourly,
            buy_vs_day_low_hourly_pct,
            price_7d_later, return_7d_pct,
            price_30d_later, return_30d_pct,
            verdict, resolution_note
        ) VALUES (
            ?, ?,
            NULL, NULL, NULL,
            NULL, NULL,
            NULL,
            NULL, NULL,
            NULL, NULL,
            ?, 'n/a'
        )
        """,
        (buy_id, _iso(evaluated_at), verdict),
    )
    conn.commit()


# ---------- generate ----------

def test_generate_empty_week_has_zero_counts(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    summary = generate_weekly_summary(memory_db, now=now)

    assert summary.signal_count == 0
    assert summary.signal_by_severity == {}
    assert summary.buy_count == 0
    assert summary.top_drop_symbol is None
    assert summary.top_drop_pct is None
    assert summary.matured_count == 0
    assert summary.verdict_counts == {}

    # Window: 7d ending at now, half-open.
    assert summary.week_end == "2026-04-11T12:00:00Z"
    assert summary.week_start == "2026-04-04T12:00:00Z"
    # Body still mentions the quiet-week marker (in Portuguese).
    assert "semana tranquila" in summary.body
    # Always includes a final conclusion line.
    assert "Leitura rápida:" in summary.body


def test_generate_counts_signals_in_window_and_breaks_down_by_severity(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    # In-window signals.
    _insert_signal(
        memory_db,
        symbol="BTCUSDT",
        detected_at=now - timedelta(days=1),
        severity="very_strong",
        score=85,
        drop_trigger_pct=8.2,
    )
    _insert_signal(
        memory_db,
        symbol="ETHUSDT",
        detected_at=now - timedelta(days=2),
        severity="strong",
        score=70,
        drop_trigger_pct=5.1,
    )
    _insert_signal(
        memory_db,
        symbol="SOLUSDT",
        detected_at=now - timedelta(days=3),
        severity="strong",
        score=66,
        drop_trigger_pct=4.0,
    )
    _insert_signal(
        memory_db,
        symbol="ADAUSDT",
        detected_at=now - timedelta(days=4),
        severity="normal",
        score=55,
        drop_trigger_pct=2.5,
    )

    # Out-of-window: one 8 days old (before start), one exactly at end.
    _insert_signal(
        memory_db,
        symbol="XRPUSDT",
        detected_at=now - timedelta(days=8),
        severity="strong",
        drop_trigger_pct=9.9,  # bigger drop, but outside the window
    )
    _insert_signal(
        memory_db,
        symbol="DOGEUSDT",
        detected_at=now,  # end is exclusive — this one must NOT count
        severity="very_strong",
        drop_trigger_pct=7.0,
    )

    summary = generate_weekly_summary(memory_db, now=now)

    assert summary.signal_count == 4
    assert summary.signal_by_severity == {
        "very_strong": 1,
        "strong": 2,
        "normal": 1,
    }
    # Top drop is the steepest IN-window drop_trigger_pct.
    assert summary.top_drop_symbol == "BTCUSDT"
    assert summary.top_drop_pct == pytest.approx(8.2)


def test_generate_top_drop_tie_breaks_on_earliest_detection(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    _insert_signal(
        memory_db,
        symbol="LATER",
        detected_at=now - timedelta(days=1),
        drop_trigger_pct=6.0,
    )
    _insert_signal(
        memory_db,
        symbol="EARLIER",
        detected_at=now - timedelta(days=3),
        drop_trigger_pct=6.0,  # same magnitude, but earlier
    )

    summary = generate_weekly_summary(memory_db, now=now)
    assert summary.top_drop_symbol == "EARLIER"
    assert summary.top_drop_pct == pytest.approx(6.0)


def test_generate_counts_buys_in_window(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=now - timedelta(days=2),
        price=50.0,
        amount_invested=100.0,
        now=now - timedelta(days=2),
    )
    insert_buy(
        memory_db,
        symbol="ETHUSDT",
        bought_at=now - timedelta(days=6),
        price=2000.0,
        amount_invested=2000.0,
        now=now - timedelta(days=6),
    )
    # Out of window (9 days old).
    insert_buy(
        memory_db,
        symbol="SOLUSDT",
        bought_at=now - timedelta(days=9),
        price=30.0,
        amount_invested=300.0,
        now=now - timedelta(days=9),
    )

    summary = generate_weekly_summary(memory_db, now=now)
    assert summary.buy_count == 2
    assert "Compras registradas: 2" in summary.body


def test_generate_aggregates_matured_verdicts_from_both_tables(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)

    # Two signal evaluations inside the window.
    sid1 = _insert_signal(
        memory_db, symbol="BTCUSDT",
        detected_at=now - timedelta(days=35),  # old parent signal
        drop_trigger_pct=4.0,
    )
    sid2 = _insert_signal(
        memory_db, symbol="ETHUSDT",
        detected_at=now - timedelta(days=35),
        drop_trigger_pct=3.0,
    )
    _insert_signal_evaluation(
        memory_db, signal_id=sid1,
        evaluated_at=now - timedelta(days=2), verdict="great",
    )
    _insert_signal_evaluation(
        memory_db, signal_id=sid2,
        evaluated_at=now - timedelta(days=1), verdict="bad",
    )

    # One signal evaluation OUTSIDE the window — must not count.
    sid3 = _insert_signal(
        memory_db, symbol="SOLUSDT",
        detected_at=now - timedelta(days=60),
    )
    _insert_signal_evaluation(
        memory_db, signal_id=sid3,
        evaluated_at=now - timedelta(days=10), verdict="good",
    )

    # One buy evaluation inside the window.
    buy = insert_buy(
        memory_db,
        symbol="ADAUSDT",
        bought_at=now - timedelta(days=40),
        price=0.5,
        amount_invested=10.0,
        now=now - timedelta(days=40),
    )
    _insert_buy_evaluation(
        memory_db, buy_id=buy.id,
        evaluated_at=now - timedelta(days=3), verdict="great",
    )

    summary = generate_weekly_summary(memory_db, now=now)
    # 2 signal evals + 1 buy eval in window = 3 matured.
    assert summary.matured_count == 3
    assert summary.verdict_counts == {"great": 2, "bad": 1}
    # Parent signals are 35d old (outside the 7d signal-count window),
    # so signal_count itself is unaffected by the matured data.
    assert summary.signal_count == 0


def test_generate_body_contains_key_facts(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    _insert_signal(
        memory_db,
        symbol="BTCUSDT",
        detected_at=now - timedelta(days=1),
        severity="very_strong",
        score=85,
        drop_trigger_pct=8.2,
    )
    _insert_signal(
        memory_db,
        symbol="ETHUSDT",
        detected_at=now - timedelta(days=2),
        severity="strong",
        drop_trigger_pct=5.0,
    )
    insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=now - timedelta(days=1),
        price=50.0,
        amount_invested=100.0,
        now=now - timedelta(days=1),
    )

    summary = generate_weekly_summary(memory_db, now=now)
    body = summary.body

    assert "📊 Resumo da semana" in body
    assert "Sinais emitidos: 2" in body
    assert "Críticos: 1" in body
    assert "Fortes: 1" in body
    # Top drop uses friendly name + dd/MM formatting.
    assert "Maior queda: BTC (-8.2%)" in body
    assert "Compras registradas: 1" in body
    # No matured evaluations → the "Avaliações vencidas" section is omitted entirely.
    assert "Avaliações vencidas" not in body
    # Conclusion line is always present.
    assert "Leitura rápida:" in body


# ---------- persist ----------

def test_persist_weekly_summary_writes_row_with_sent_zero(memory_db):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    summary = WeeklySummary(
        week_start="2026-04-04T12:00:00Z",
        week_end="2026-04-11T12:00:00Z",
        signal_count=3,
        signal_by_severity={"strong": 2, "normal": 1},
        buy_count=1,
        top_drop_symbol="BTCUSDT",
        top_drop_pct=8.2,
        matured_count=0,
        verdict_counts={},
        body="hand-crafted body",
    )

    summary_id = persist_weekly_summary(memory_db, summary, now=now)
    assert summary_id > 0

    row = memory_db.execute(
        "SELECT * FROM weekly_summaries WHERE id = ?", (summary_id,)
    ).fetchone()
    assert row is not None
    assert row["week_start"] == "2026-04-04T12:00:00Z"
    assert row["week_end"] == "2026-04-11T12:00:00Z"
    assert row["generated_at"] == "2026-04-11T12:00:00Z"
    assert row["body"] == "hand-crafted body"
    assert row["signal_count"] == 3
    assert row["buy_count"] == 1
    assert row["top_drop_symbol"] == "BTCUSDT"
    assert row["top_drop_pct"] == pytest.approx(8.2)
    assert row["sent"] == 0


# ---------- send ----------

def test_send_weekly_summary_success_marks_sent(memory_db, ntfy_settings):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    summary = generate_weekly_summary(memory_db, now=now)
    summary_id = persist_weekly_summary(memory_db, summary, now=now)

    sender = _RecordingSender(SendResult(sent=True, reason=REASON_SENT, status_code=200))
    result = send_weekly_summary(
        memory_db, summary_id, ntfy=ntfy_settings, sender=sender
    )

    assert result.sent is True
    assert len(sender.calls) == 1
    call = sender.calls[0]
    # Title carries the window in dd/MM format; body is the persisted body.
    assert "04/04" in call.title
    assert "11/04" in call.title
    assert "Resumo semanal" in call.title
    assert call.body == summary.body
    assert call.priority == "default"
    assert "weekly" in call.tags

    sent_flag = memory_db.execute(
        "SELECT sent FROM weekly_summaries WHERE id = ?", (summary_id,)
    ).fetchone()[0]
    assert sent_flag == 1


def test_send_weekly_summary_failure_leaves_sent_zero(memory_db, ntfy_settings):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    summary = generate_weekly_summary(memory_db, now=now)
    summary_id = persist_weekly_summary(memory_db, summary, now=now)

    sender = _RecordingSender(
        SendResult(sent=False, reason=REASON_NETWORK_ERROR, error="boom")
    )
    result = send_weekly_summary(
        memory_db, summary_id, ntfy=ntfy_settings, sender=sender
    )

    assert result.sent is False
    sent_flag = memory_db.execute(
        "SELECT sent FROM weekly_summaries WHERE id = ?", (summary_id,)
    ).fetchone()[0]
    assert sent_flag == 0


def test_send_weekly_summary_unknown_id_raises(memory_db, ntfy_settings):
    sender = _RecordingSender(SendResult(sent=True, reason=REASON_SENT, status_code=200))
    with pytest.raises(ValueError, match="not found"):
        send_weekly_summary(
            memory_db, 9999, ntfy=ntfy_settings, sender=sender
        )
    assert sender.calls == []


# ---------- end-to-end ----------

def test_generate_and_send_weekly_summary_end_to_end(memory_db, ntfy_settings):
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    _insert_signal(
        memory_db,
        symbol="BTCUSDT",
        detected_at=now - timedelta(days=2),
        severity="strong",
        drop_trigger_pct=6.5,
    )
    insert_buy(
        memory_db,
        symbol="BTCUSDT",
        bought_at=now - timedelta(days=1),
        price=50.0,
        amount_invested=100.0,
        now=now - timedelta(days=1),
    )

    sender = _RecordingSender(SendResult(sent=True, reason=REASON_SENT, status_code=200))
    run = generate_and_send_weekly_summary(
        memory_db, ntfy=ntfy_settings, now=now, sender=sender
    )

    assert run.summary.signal_count == 1
    assert run.summary.buy_count == 1
    assert run.summary.top_drop_symbol == "BTCUSDT"
    assert run.send_result.sent is True
    assert run.summary_id > 0

    # Row persisted and marked sent=1.
    row = memory_db.execute(
        "SELECT sent, body FROM weekly_summaries WHERE id = ?",
        (run.summary_id,),
    ).fetchone()
    assert row["sent"] == 1
    assert row["body"] == run.summary.body

    # Sender was called exactly once with the persisted body.
    assert len(sender.calls) == 1
    assert sender.calls[0].body == run.summary.body
