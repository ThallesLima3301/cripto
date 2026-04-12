"""Tests for `crypto_monitor.notifications.service`.

These tests drive `process_pending_signals` and `flush_queue` end-to-end
against an in-memory SQLite database. The ntfy sender is stubbed so no
HTTP is attempted — we pass a `sender` callable that records calls
and returns a scripted SendResult.

Signal rows are inserted directly rather than scored via the engine,
because the service layer's contract starts at "a row exists in
`signals` with alerted=0" — the test does not need to re-exercise
the scoring engine.

Timezone throughout is `America/Sao_Paulo` (UTC-3), matching the
policy tests, so quiet hours 22..8 local = 01:00..11:00 UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from crypto_monitor.notifications.ntfy import (
    REASON_NETWORK_ERROR,
    REASON_SENT,
    SendResult,
)
from crypto_monitor.notifications.service import (
    flush_queue,
    process_pending_signals,
)


UTC = timezone.utc
TZ = "America/Sao_Paulo"


class RecordingSender:
    """Stub `send_ntfy` that records calls and returns scripted results."""

    def __init__(self, results: list[SendResult] | None = None) -> None:
        self._results = list(results) if results else []
        self._default = SendResult(sent=True, reason=REASON_SENT, status_code=200)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        ntfy,
        title: str,
        body: str,
        *,
        priority: str,
        tags: tuple[str, ...],
    ) -> SendResult:
        self.calls.append(
            {
                "title": title,
                "body": body,
                "priority": priority,
                "tags": tags,
            }
        )
        if self._results:
            return self._results.pop(0)
        return self._default


# ---------- signal inserter ----------

def _insert_signal(
    conn,
    *,
    symbol: str = "BTCUSDT",
    severity: str = "strong",
    score: int = 72,
    candle_hour: str = "2026-04-11T14:00:00Z",
    detected_at: str = "2026-04-11T14:05:00Z",
    price: float = 40.0,
    trigger_reason: str = "7d drop 25.9%",
    dominant_tf: str | None = "7d",
    drop_pct: float | None = 25.9,
    rsi_1h: float | None = 12.0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, dominant_trigger_timeframe,
            drop_trigger_pct, rsi_1h, reversal_signal, score_breakdown
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}')
        """,
        (
            symbol,
            detected_at,
            candle_hour,
            price,
            score,
            severity,
            trigger_reason,
            dominant_tf,
            drop_pct,
            rsi_1h,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _signal_row(conn, signal_id: int):
    return conn.execute(
        "SELECT alerted, alert_skipped_reason FROM signals WHERE id = ?",
        (signal_id,),
    ).fetchone()


def _notif_rows(conn, signal_id: int):
    return conn.execute(
        "SELECT id, symbol, title, body, priority, tags, queued, "
        "bypass_quiet, delivered, sent_at, delivery_attempts, last_error "
        "FROM notifications WHERE signal_id = ? ORDER BY id ASC",
        (signal_id,),
    ).fetchall()


# ---------- send now ----------

def test_send_now_writes_delivered_row_and_clears_skipped_reason(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db, severity="strong", score=72)
    # 18:00 UTC = 15:00 local → outside quiet hours.
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    sender = RecordingSender()

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=now,
        sender=sender,
    )

    assert report.considered == 1
    assert report.sent == 1
    assert report.queued == 0
    assert report.skipped_cooldown == 0

    sig = _signal_row(memory_db, sid)
    assert sig["alerted"] == 1
    # Successful live sends must NOT populate alert_skipped_reason.
    assert sig["alert_skipped_reason"] is None

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["delivered"] == 1
    assert row["queued"] == 0
    assert row["sent_at"] is not None
    assert row["last_error"] is None
    assert row["priority"] == "high"

    # Title/body should include the formatted richer content.
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert "BTCUSDT" in call["title"]
    assert "STRONG 72" in call["title"]
    assert "-25.9% 7d" in call["title"]
    assert "RSI1h 12" in call["body"]


# ---------- queue (quiet hours) ----------

def test_queue_during_quiet_hours(memory_db, alerts_settings, ntfy_settings):
    sid = _insert_signal(memory_db, severity="strong", score=72)
    # 04:00 UTC = 01:00 local → quiet hours.
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    sender = RecordingSender()

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=now,
        sender=sender,
    )

    assert report.queued == 1
    assert report.sent == 0
    # Nothing should have been POSTed.
    assert sender.calls == []

    sig = _signal_row(memory_db, sid)
    assert sig["alerted"] == 1
    assert sig["alert_skipped_reason"] == "quiet_hours"

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["queued"] == 1
    assert row["delivered"] == 0
    assert row["sent_at"] is None
    assert row["bypass_quiet"] == 0


# ---------- very_strong bypasses quiet hours ----------

def test_very_strong_bypasses_quiet_hours(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db, severity="very_strong", score=85)
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)  # quiet hours
    sender = RecordingSender()

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=now,
        sender=sender,
    )

    assert report.sent == 1
    assert report.queued == 0

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["delivered"] == 1
    assert row["bypass_quiet"] == 1

    sig = _signal_row(memory_db, sid)
    assert sig["alert_skipped_reason"] is None
    assert len(sender.calls) == 1
    assert sender.calls[0]["priority"] == "max"


# ---------- cooldown skip ----------

def test_cooldown_skip_does_not_insert_notification(
    memory_db, alerts_settings, ntfy_settings
):
    # First signal sends live at 15:00 local.
    first_now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    sid1 = _insert_signal(
        memory_db,
        severity="strong",
        score=70,
        candle_hour="2026-04-11T14:00:00Z",
        detected_at="2026-04-11T14:05:00Z",
    )
    sender = RecordingSender()
    process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=first_now,
        sender=sender,
    )

    # Second signal 30 minutes later, score +2 only → under the cooldown.
    sid2 = _insert_signal(
        memory_db,
        severity="strong",
        score=72,
        candle_hour="2026-04-11T15:00:00Z",
        detected_at="2026-04-11T15:35:00Z",
    )
    second_now = first_now.replace(hour=18, minute=30)

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=second_now,
        sender=sender,
    )

    assert report.considered == 1
    assert report.skipped_cooldown == 1
    assert report.sent == 0

    sig2 = _signal_row(memory_db, sid2)
    assert sig2["alerted"] == 1
    assert sig2["alert_skipped_reason"] is not None
    assert sig2["alert_skipped_reason"].startswith("cooldown:")

    # No notification row was inserted for the skipped signal.
    assert _notif_rows(memory_db, sid2) == []
    # Only the first signal's delivery call was made.
    assert len(sender.calls) == 1
    assert sid1  # sanity


# ---------- escalation override ----------

def test_escalation_jump_overrides_cooldown(
    memory_db, alerts_settings, ntfy_settings
):
    first_now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    sid1 = _insert_signal(
        memory_db,
        severity="normal",
        score=55,
        candle_hour="2026-04-11T14:00:00Z",
        detected_at="2026-04-11T14:05:00Z",
    )
    sender = RecordingSender()
    process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=first_now,
        sender=sender,
    )

    # Same symbol, 30min later, score jumps from 55 -> 75 = +20 >= 10.
    sid2 = _insert_signal(
        memory_db,
        severity="strong",
        score=75,
        candle_hour="2026-04-11T15:00:00Z",
        detected_at="2026-04-11T15:35:00Z",
    )
    second_now = first_now.replace(minute=30)

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=second_now,
        sender=sender,
    )

    assert report.sent == 1
    assert report.skipped_cooldown == 0

    notifs = _notif_rows(memory_db, sid2)
    assert len(notifs) == 1
    assert notifs[0]["delivered"] == 1

    sig2 = _signal_row(memory_db, sid2)
    assert sig2["alert_skipped_reason"] is None
    assert sid1  # sanity — first signal stays delivered


# ---------- send failure ----------

def test_send_failure_records_failed_row_and_marks_skipped_reason(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db, severity="strong", score=72)
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    sender = RecordingSender(
        [SendResult(sent=False, reason=REASON_NETWORK_ERROR, error="boom")]
    )

    report = process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=now,
        sender=sender,
    )

    assert report.send_failed == 1
    assert report.sent == 0

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["delivered"] == 0
    assert row["queued"] == 0
    assert row["sent_at"] is None
    assert row["last_error"] is not None
    assert "network_error" in row["last_error"]

    sig = _signal_row(memory_db, sid)
    # Signal is still marked alerted=1 (v1 tradeoff: no auto-retry).
    assert sig["alerted"] == 1
    assert sig["alert_skipped_reason"] == "send_failed:network_error"


# ---------- flush_queue ----------

def test_flush_queue_sends_pending_rows_after_quiet_hours(
    memory_db, alerts_settings, ntfy_settings
):
    # Queue a signal during quiet hours.
    sid = _insert_signal(memory_db, severity="strong", score=72)
    quiet_now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    sender = RecordingSender()
    process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=quiet_now,
        sender=sender,
    )
    assert sender.calls == []  # sanity: queued, not sent

    # Now it's 18:00 UTC = 15:00 local, outside quiet hours. Flush.
    day_now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    report = flush_queue(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=day_now,
        sender=sender,
    )

    assert report.in_quiet_hours is False
    assert report.considered == 1
    assert report.sent == 1
    assert report.failed == 0

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["delivered"] == 1
    assert row["queued"] == 0
    assert row["sent_at"] is not None
    assert row["delivery_attempts"] == 1
    assert row["last_error"] is None

    # Sender saw exactly the queued row.
    assert len(sender.calls) == 1
    assert "STRONG 72" in sender.calls[0]["title"]


def test_flush_queue_during_quiet_hours_is_a_noop(
    memory_db, alerts_settings, ntfy_settings
):
    # Queue a signal, then try to flush while still in quiet hours.
    _insert_signal(memory_db, severity="strong", score=72)
    quiet_now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    sender = RecordingSender()
    process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=quiet_now,
        sender=sender,
    )

    still_quiet = datetime(2026, 4, 11, 5, 0, tzinfo=UTC)  # 02:00 local
    report = flush_queue(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=still_quiet,
        sender=sender,
    )

    assert report.in_quiet_hours is True
    assert report.considered == 0
    assert report.sent == 0
    # Sender was NOT called during flush.
    assert sender.calls == []


def test_flush_queue_failure_leaves_row_queued_and_bumps_attempts(
    memory_db, alerts_settings, ntfy_settings
):
    _insert_signal(memory_db, severity="strong", score=72)
    quiet_now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    process_pending_signals(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=quiet_now,
        sender=RecordingSender(),  # queues
    )

    day_now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    failing = RecordingSender(
        [SendResult(sent=False, reason=REASON_NETWORK_ERROR, error="down")]
    )
    report = flush_queue(
        memory_db,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        timezone_name=TZ,
        now=day_now,
        sender=failing,
    )

    assert report.failed == 1
    assert report.sent == 0

    row = memory_db.execute(
        "SELECT queued, delivered, delivery_attempts, last_error "
        "FROM notifications"
    ).fetchone()
    assert row["queued"] == 1  # still queued → retried next flush
    assert row["delivered"] == 0
    assert row["delivery_attempts"] == 1
    assert row["last_error"] is not None
    assert "network_error" in row["last_error"]
