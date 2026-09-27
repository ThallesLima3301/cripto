"""Tests for `crypto_monitor.notifications.service`.

These tests drive `process_pending_signals` and `flush_queue` end-to-end
against isolated SQLite databases, including a temporary file for restart
coverage. The ntfy sender is stubbed so no
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

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db
from crypto_monitor.notifications.ntfy import (
    REASON_HTTP_ERROR,
    REASON_MISSING_TOPIC,
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

    # Title/body should include the formatted decision-oriented content.
    assert len(sender.calls) == 1
    call = sender.calls[0]
    # Client mode: friendly name in title, decision phrase, no raw pair.
    assert "BTC" in call["title"]
    assert "Vale observar" in call["title"]
    assert "BTCUSDT" not in call["title"]
    # Body shows price, 24h variation, and reason bullets.
    assert "BTC @" in call["body"]
    assert "•" in call["body"]


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

def test_send_failure_queues_failed_row_and_marks_skipped_reason(
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
    assert report.queued == 1

    notifs = _notif_rows(memory_db, sid)
    assert len(notifs) == 1
    row = notifs[0]
    assert row["delivered"] == 0
    assert row["queued"] == 1
    assert row["delivery_attempts"] == 1
    assert row["sent_at"] is None
    assert row["last_error"] is not None
    assert "network_error" in row["last_error"]

    sig = _signal_row(memory_db, sid)
    # The existing notification is retried; the signal is not reprocessed.
    assert sig["alerted"] == 1
    assert sig["alert_skipped_reason"] == "send_failed:network_error"


@pytest.mark.parametrize("status_code", [None, 408, 429, 500, 503, 599])
def test_transient_http_failure_is_retried_on_the_existing_notification(
    memory_db, alerts_settings, ntfy_settings, status_code
):
    sid = _insert_signal(memory_db)
    now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    failure = SendResult(
        sent=False, reason=REASON_HTTP_ERROR,
        status_code=status_code, error=f"HTTP {status_code}",
    )
    sender = RecordingSender([failure, failure])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )

    initial = process_pending_signals(memory_db, now=now, **kwargs)
    row_id = _notif_rows(memory_db, sid)[0]["id"]
    assert initial.queued == initial.send_failed == 1

    failed = flush_queue(memory_db, now=now + timedelta(minutes=5), **kwargs)
    row = _notif_rows(memory_db, sid)[0]
    assert failed.considered == failed.failed == 1
    assert failed.sent == failed.discarded == 0
    assert row["queued"] == 1
    assert row["delivery_attempts"] == 2

    succeeded = flush_queue(memory_db, now=now + timedelta(minutes=10), **kwargs)
    rows = _notif_rows(memory_db, sid)
    assert succeeded.considered == succeeded.sent == 1
    assert succeeded.failed == succeeded.discarded == 0
    assert len(rows) == 1
    assert rows[0]["id"] == row_id
    assert rows[0]["delivery_attempts"] == 3
    assert rows[0]["queued"] == 0
    assert rows[0]["delivered"] == 1
    assert rows[0]["last_error"] is None
    assert _signal_row(memory_db, sid)["alert_skipped_reason"] is None


@pytest.mark.parametrize(
    "failure",
    [
        SendResult(False, REASON_MISSING_TOPIC, error="topic absent"),
        SendResult(False, REASON_HTTP_ERROR, status_code=302, error="redirect"),
        SendResult(False, REASON_HTTP_ERROR, status_code=400, error="bad request"),
        SendResult(False, REASON_HTTP_ERROR, status_code=401, error="unauthorized"),
        SendResult(False, REASON_HTTP_ERROR, status_code=403, error="forbidden"),
    ],
    ids=["missing-topic", "redirect", "bad-request", "unauthorized", "forbidden"],
)
@pytest.mark.parametrize("initially_queued", [False, True], ids=["live", "queued"])
def test_permanent_failure_is_retained_without_future_delivery_attempts(
    memory_db, alerts_settings, ntfy_settings, failure, initially_queued
):
    sid = _insert_signal(memory_db)
    now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    sender = RecordingSender([failure])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    if initially_queued:
        process_pending_signals(memory_db, now=now.replace(hour=4), **kwargs)
        report = flush_queue(memory_db, now=now, **kwargs)
        assert report.considered == report.failed == report.discarded == 1
    else:
        report = process_pending_signals(memory_db, now=now, **kwargs)
        assert report.send_failed == 1
        assert report.queued == 0

    row = _notif_rows(memory_db, sid)[0]
    assert row["queued"] == row["delivered"] == 0
    assert row["sent_at"] is None
    assert row["delivery_attempts"] == 1
    assert failure.reason in row["last_error"]
    assert failure.error in row["last_error"]

    assert process_pending_signals(memory_db, now=now, **kwargs).considered == 0
    assert flush_queue(memory_db, now=now, **kwargs).considered == 0
    assert len(sender.calls) == 1
    assert len(_notif_rows(memory_db, sid)) == 1


def test_retry_survives_database_reopen_and_does_not_duplicate_delivery(
    tmp_path, alerts_settings, ntfy_settings
):
    path = tmp_path / "notifications.db"
    now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    sender = RecordingSender([
        SendResult(False, REASON_NETWORK_ERROR, error="connection lost"),
    ])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    conn = get_connection(path)
    try:
        init_db(conn)
        run_migrations(conn)
        sid = _insert_signal(conn)
        process_pending_signals(conn, now=now, **kwargs)
        row_id = _notif_rows(conn, sid)[0]["id"]
        assert process_pending_signals(conn, now=now, **kwargs).considered == 0
        assert len(sender.calls) == 1
    finally:
        conn.close()

    conn = get_connection(path)
    try:
        report = flush_queue(conn, now=now + timedelta(minutes=30), **kwargs)
        assert report.sent == 1
        rows = _notif_rows(conn, sid)
        assert len(rows) == 1
        assert rows[0]["id"] == row_id
        assert rows[0]["delivered"] == 1
        assert rows[0]["queued"] == 0
        assert rows[0]["delivery_attempts"] == 2
        assert rows[0]["sent_at"] == "2026-04-11T18:30:00Z"
        assert rows[0]["last_error"] is None
        assert _signal_row(conn, sid)["alerted"] == 1
        assert _signal_row(conn, sid)["alert_skipped_reason"] is None
    finally:
        conn.close()

    conn = get_connection(path)
    try:
        later = now + timedelta(hours=1)
        assert process_pending_signals(conn, now=later, **kwargs).considered == 0
        assert flush_queue(conn, now=later, **kwargs).considered == 0
        assert len(sender.calls) == 2
    finally:
        conn.close()


@pytest.mark.parametrize("severity", ["strong", "very_strong"])
def test_retry_during_quiet_hours_respects_the_original_signal_severity(
    memory_db, alerts_settings, ntfy_settings, severity
):
    sid = _insert_signal(memory_db, severity=severity, score=85)
    day_now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    sender = RecordingSender([
        SendResult(False, REASON_NETWORK_ERROR, error="temporary outage"),
    ])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=day_now, **kwargs)
    quiet_now = day_now + timedelta(hours=10)
    report = flush_queue(memory_db, now=quiet_now, **kwargs)

    assert report.in_quiet_hours is True
    assert report.failed == report.discarded == 0
    row = _notif_rows(memory_db, sid)[0]
    if severity == "very_strong":
        assert report.considered == report.sent == 1
        assert row["queued"] == 0
        assert row["delivered"] == 1
        assert row["delivery_attempts"] == 2
        assert len(sender.calls) == 2
    else:
        assert report.considered == report.sent == 0
        assert row["queued"] == 1
        assert row["delivered"] == 0
        assert row["delivery_attempts"] == 1
        assert len(sender.calls) == 1


def test_failed_very_strong_alert_can_retry_while_quiet_hours_continue(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db, severity="very_strong", score=85)
    now = datetime(2026, 4, 11, 4, tzinfo=UTC)
    sender = RecordingSender([
        SendResult(False, REASON_NETWORK_ERROR, error="temporary outage"),
    ])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=now, **kwargs)
    report = flush_queue(memory_db, now=now + timedelta(minutes=30), **kwargs)

    assert report.in_quiet_hours is True
    assert report.considered == report.sent == 1
    assert _notif_rows(memory_db, sid)[0]["delivered"] == 1
    assert len(sender.calls) == 2


def test_repeated_failures_stop_after_five_total_delivery_attempts(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db)
    now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    failure = SendResult(False, REASON_NETWORK_ERROR, error="still offline")
    sender = RecordingSender([failure] * 5)
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=now, **kwargs)
    for attempt in range(2, 6):
        report = flush_queue(memory_db, now=now + timedelta(minutes=attempt), **kwargs)
        row = _notif_rows(memory_db, sid)[0]
        assert report.considered == report.failed == 1
        assert report.sent == 0
        assert report.discarded == int(attempt == 5)
        assert row["delivery_attempts"] == attempt
        assert row["queued"] == int(attempt < 5)

    assert row["delivered"] == 0
    assert row["sent_at"] is None
    assert row["last_error"].startswith("retry_exhausted:")
    assert "network_error" in row["last_error"]
    assert "still offline" in row["last_error"]
    assert flush_queue(memory_db, now=now + timedelta(minutes=10), **kwargs).considered == 0
    assert len(sender.calls) == 5
    assert len(_notif_rows(memory_db, sid)) == 1


@pytest.mark.parametrize("severity", ["strong", "very_strong"])
def test_queued_alert_expires_at_24_hours_even_during_quiet_hours(
    memory_db, alerts_settings, ntfy_settings, severity
):
    sid = _insert_signal(memory_db, severity=severity, score=85)
    now = datetime(2026, 4, 11, 4, tzinfo=UTC)
    sender = RecordingSender([
        SendResult(False, REASON_NETWORK_ERROR, error="original outage"),
    ])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=now, **kwargs)
    initial_calls = len(sender.calls)
    report = flush_queue(memory_db, now=now + timedelta(hours=24), **kwargs)

    assert report.in_quiet_hours is True
    assert report.considered == report.sent == report.failed == 0
    assert report.discarded == 1
    row = _notif_rows(memory_db, sid)[0]
    assert row["queued"] == row["delivered"] == 0
    assert row["sent_at"] is None
    assert row["delivery_attempts"] == initial_calls
    assert row["last_error"].startswith("retry_expired")
    if initial_calls:
        assert "network_error" in row["last_error"]
        assert "original outage" in row["last_error"]
    assert len(sender.calls) == initial_calls
    assert flush_queue(memory_db, now=now + timedelta(hours=25), **kwargs).discarded == 0


def test_retry_remains_eligible_just_before_24_hour_expiry(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db)
    now = datetime(2026, 4, 11, 18, tzinfo=UTC)
    sender = RecordingSender([
        SendResult(False, REASON_NETWORK_ERROR, error="outage"),
    ])
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=now, **kwargs)
    report = flush_queue(
        memory_db, now=now + timedelta(hours=24, seconds=-1), **kwargs,
    )
    assert report.sent == 1
    assert report.discarded == 0
    assert _notif_rows(memory_db, sid)[0]["delivered"] == 1


def test_already_exhausted_queued_row_is_discarded_without_sending(
    memory_db, alerts_settings, ntfy_settings
):
    sid = _insert_signal(memory_db)
    now = datetime(2026, 4, 11, 4, tzinfo=UTC)
    sender = RecordingSender()
    kwargs = dict(
        alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ,
        sender=sender,
    )
    process_pending_signals(memory_db, now=now, **kwargs)
    # Older versions allowed unlimited queued attempts; stop those rows too.
    memory_db.execute(
        "UPDATE notifications SET delivery_attempts = 5, last_error = ?",
        ("network_error:prior failure",),
    )
    memory_db.commit()
    report = flush_queue(memory_db, now=now + timedelta(hours=1), **kwargs)

    assert report.considered == report.sent == report.failed == 0
    assert report.discarded == 1
    row = _notif_rows(memory_db, sid)[0]
    assert row["queued"] == row["delivered"] == 0
    assert row["delivery_attempts"] == 5
    assert row["last_error"].startswith("retry_exhausted:")
    assert "prior failure" in row["last_error"]
    assert sender.calls == []


# ---------- flush_queue ----------


@pytest.mark.parametrize("queued", [False, True])
def test_sent_timestamp_is_sampled_after_server_acknowledgement(
    memory_db, alerts_settings, ntfy_settings, queued,
):
    sid = _insert_signal(memory_db)
    start = datetime(2026, 4, 11, 18, 59, tzinfo=UTC)
    acknowledged = start + timedelta(minutes=2)
    events = []
    kwargs = dict(alerts=alerts_settings, ntfy=ntfy_settings, timezone_name=TZ)
    if queued:
        process_pending_signals(
            memory_db, now=start.replace(hour=4), sender=RecordingSender(), **kwargs,
        )

    def sender(*args, **kw):
        events.append("send")
        return SendResult(True, REASON_SENT)

    def clock():
        assert events == ["send"]
        events.append("clock")
        return acknowledged

    action = flush_queue if queued else process_pending_signals
    report = action(memory_db, now=start, sender=sender, sent_clock=clock, **kwargs)
    assert report.sent == 1
    assert _notif_rows(memory_db, sid)[0]["sent_at"] == "2026-04-11T19:01:00Z"
    assert events == ["send", "clock"]

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

    # Preserve provenance for notifications originally deferred overnight.
    assert _signal_row(memory_db, sid)["alert_skipped_reason"] == "quiet_hours"

    # Sender saw exactly the queued row.
    assert len(sender.calls) == 1
    # Queued notifications carry the decision-oriented title format.
    assert "Vale observar" in sender.calls[0]["title"]


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
