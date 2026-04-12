"""Notification orchestrator.

This module walks unalerted rows in the `signals` table, asks
`policy.decide_alert` what to do with each, and executes the
decision:

  * `send_now`         — POST via `ntfy.send_ntfy`, then write a
                         notifications row with delivered=1 (on
                         success) or delivered=0+last_error (on
                         failure). The signal is marked `alerted=1`
                         either way so we do not retry automatically
                         — a failing ntfy server would otherwise
                         generate one send attempt per scan cycle.

                         **v1 tradeoff**: because a send failure
                         still flips `alerted=1`, a transient
                         network outage during a scan will
                         permanently suppress that specific signal
                         on the phone. The row is still in the
                         `signals` table and the failed notification
                         is visible in the `notifications` table
                         (with `last_error` set), so the user can
                         inspect it — but we do not auto-replay. If
                         that becomes painful we can add a
                         short-window retry queue later without
                         touching the policy layer.

  * `queue`            — write a notifications row with delivered=0,
                         queued=1, sent_at=NULL. These rows are the
                         pending-delivery queue and are picked up by
                         `flush_queue` once quiet hours end.

  * `skip_cooldown`    — do NOT write a notifications row; flip
                         `signals.alerted=1` with
                         `alert_skipped_reason=cooldown:...` so the
                         row does not get re-examined on the next scan.

`flush_queue` is the companion: when quiet hours end (or when the
scheduler fires `flush` explicitly), it selects every notifications
row with `delivered=0 AND queued=1`, checks that we're NOT currently
in quiet hours, and sends each one. Successful rows are stamped with
`sent_at`, `delivered=1`, `queued=0`. Failed rows keep `queued=1` so
the next flush will try again.

Scoping note
------------
Block 7 does NOT touch ingestion, does NOT ship a `scan()` entry point,
and does NOT hook into the scheduler. Wiring candle fetch → scoring →
notification is Block 10's responsibility. The functions here can be
called directly from tests or from the eventual scheduler without any
glue beyond passing a connection, settings, and a clock.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from crypto_monitor.config.settings import AlertSettings, NtfySettings
from crypto_monitor.notifications.ntfy import (
    REASON_SENT,
    SendResult,
    send_ntfy,
)
from crypto_monitor.notifications.policy import (
    ACTION_QUEUE,
    ACTION_SEND_NOW,
    ACTION_SKIP_COOLDOWN,
    AlertDecision,
    PriorAlert,
    SignalFacts,
    decide_alert,
)
from crypto_monitor.utils.time_utils import (
    from_utc_iso,
    is_quiet_hours,
    now_utc,
    to_utc_iso,
)


logger = logging.getLogger(__name__)


# Signature for an injected ntfy sender — tests pass a stub that does
# not touch the network. The default delegates to `send_ntfy`.
NtfySender = Callable[..., SendResult]


# ntfy priority mapping. Kept local to the service layer because it's
# specific to how we translate our severity ladder into the ntfy API.
_PRIORITY_BY_SEVERITY: dict[str, str] = {
    "normal": "default",
    "strong": "high",
    "very_strong": "max",
}


@dataclass(frozen=True)
class ProcessReport:
    """Summary of a `process_pending_signals` run."""
    considered: int
    sent: int
    queued: int
    skipped_cooldown: int
    send_failed: int


@dataclass(frozen=True)
class FlushReport:
    """Summary of a `flush_queue` run."""
    considered: int
    sent: int
    failed: int
    in_quiet_hours: bool


# ---------- main entry points ----------

def process_pending_signals(
    conn: sqlite3.Connection,
    *,
    alerts: AlertSettings,
    ntfy: NtfySettings,
    timezone_name: str,
    now: datetime | None = None,
    sender: NtfySender | None = None,
) -> ProcessReport:
    """Walk unalerted signals and dispatch each via the alert policy.

    `now` and `sender` are injectable for testing. In production
    `now` defaults to `now_utc()` and `sender` defaults to
    `ntfy.send_ntfy`.
    """
    if now is None:
        now = now_utc()
    if sender is None:
        sender = send_ntfy

    rows = conn.execute(
        """
        SELECT id, symbol, severity, score, candle_hour,
               price_at_signal, trigger_reason, detected_at,
               dominant_trigger_timeframe, drop_trigger_pct,
               rsi_1h
        FROM signals
        WHERE alerted = 0
        ORDER BY detected_at ASC, id ASC
        """
    ).fetchall()

    considered = len(rows)
    sent = 0
    queued = 0
    skipped_cooldown = 0
    send_failed = 0

    for row in rows:
        facts = SignalFacts(
            signal_id=row["id"],
            symbol=row["symbol"],
            severity=row["severity"],
            score=row["score"],
            candle_hour=row["candle_hour"],
        )
        prior = _load_prior_alert(conn, facts.symbol)
        decision = decide_alert(facts, prior, now, alerts, timezone_name)

        if decision.action == ACTION_SKIP_COOLDOWN:
            _mark_signal_alerted(conn, facts.signal_id, decision.reason)
            skipped_cooldown += 1
            continue

        title, body = _format_message(
            symbol=facts.symbol,
            severity=facts.severity,
            score=facts.score,
            price=row["price_at_signal"],
            trigger_reason=row["trigger_reason"],
            dominant_tf=row["dominant_trigger_timeframe"],
            drop_pct=row["drop_trigger_pct"],
            rsi_1h=row["rsi_1h"],
        )
        priority = _PRIORITY_BY_SEVERITY.get(facts.severity, "default")
        tags = tuple(ntfy.default_tags)

        if decision.action == ACTION_QUEUE:
            _insert_queued_notification(
                conn,
                signal_id=facts.signal_id,
                symbol=facts.symbol,
                title=title,
                body=body,
                priority=priority,
                tags=tags,
                created_at=now,
                bypass_quiet=decision.override_quiet_hours,
            )
            # Stable "quiet_hours" tag rather than the policy's
            # free-form reason, so downstream filters stay trivial.
            _mark_signal_alerted(conn, facts.signal_id, "quiet_hours")
            queued += 1
            continue

        # ACTION_SEND_NOW
        result = sender(
            ntfy,
            title,
            body,
            priority=priority,
            tags=tags,
        )
        if result.sent:
            _insert_delivered_notification(
                conn,
                signal_id=facts.signal_id,
                symbol=facts.symbol,
                title=title,
                body=body,
                priority=priority,
                tags=tags,
                created_at=now,
                sent_at=now,
                bypass_quiet=decision.override_quiet_hours,
            )
            # Clean live-send: alert_skipped_reason stays NULL.
            _mark_signal_alerted(conn, facts.signal_id, None)
            sent += 1
        else:
            _insert_failed_notification(
                conn,
                signal_id=facts.signal_id,
                symbol=facts.symbol,
                title=title,
                body=body,
                priority=priority,
                tags=tags,
                created_at=now,
                bypass_quiet=decision.override_quiet_hours,
                last_error=f"{result.reason}:{result.error or ''}",
            )
            _mark_signal_alerted(
                conn, facts.signal_id, f"send_failed:{result.reason}"
            )
            send_failed += 1

    conn.commit()
    return ProcessReport(
        considered=considered,
        sent=sent,
        queued=queued,
        skipped_cooldown=skipped_cooldown,
        send_failed=send_failed,
    )


def flush_queue(
    conn: sqlite3.Connection,
    *,
    alerts: AlertSettings,
    ntfy: NtfySettings,
    timezone_name: str,
    now: datetime | None = None,
    sender: NtfySender | None = None,
) -> FlushReport:
    """Drain pending queued notifications, unless we're in quiet hours.

    Returns a report including `in_quiet_hours` so the caller can
    distinguish "queue is empty" from "quiet hours are still active".
    """
    if now is None:
        now = now_utc()
    if sender is None:
        sender = send_ntfy

    if is_quiet_hours(
        now,
        timezone_name,
        alerts.quiet_hours_start,
        alerts.quiet_hours_end,
    ):
        return FlushReport(
            considered=0, sent=0, failed=0, in_quiet_hours=True
        )

    rows = conn.execute(
        """
        SELECT id, symbol, title, body, priority, tags
        FROM notifications
        WHERE delivered = 0 AND queued = 1
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    considered = len(rows)
    sent = 0
    failed = 0

    for row in rows:
        tags_csv = row["tags"] or ""
        tags = tuple(t for t in tags_csv.split(",") if t)
        result = sender(
            ntfy,
            row["title"],
            row["body"],
            priority=row["priority"],
            tags=tags,
        )
        if result.sent:
            conn.execute(
                """
                UPDATE notifications
                SET delivered = 1,
                    queued = 0,
                    sent_at = ?,
                    delivery_attempts = delivery_attempts + 1,
                    last_error = NULL
                WHERE id = ?
                """,
                (to_utc_iso(now), row["id"]),
            )
            sent += 1
        else:
            conn.execute(
                """
                UPDATE notifications
                SET delivery_attempts = delivery_attempts + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (f"{result.reason}:{result.error or ''}", row["id"]),
            )
            failed += 1

    conn.commit()
    return FlushReport(
        considered=considered,
        sent=sent,
        failed=failed,
        in_quiet_hours=False,
    )


# ---------- internals ----------

def _load_prior_alert(
    conn: sqlite3.Connection, symbol: str
) -> PriorAlert | None:
    """Return the most recent DELIVERED alert for this symbol, if any.

    We JOIN back to `signals` to recover the score/severity of the
    signal that triggered the prior alert — the notifications row
    itself only stores the rendered title/body.
    """
    row = conn.execute(
        """
        SELECT n.sent_at, s.score, s.severity
        FROM notifications n
        JOIN signals s ON s.id = n.signal_id
        WHERE n.symbol = ?
          AND n.delivered = 1
          AND n.sent_at IS NOT NULL
        ORDER BY n.sent_at DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return PriorAlert(
        sent_at=from_utc_iso(row["sent_at"]),
        score=int(row["score"]),
        severity=row["severity"],
    )


def _mark_signal_alerted(
    conn: sqlite3.Connection,
    signal_id: int,
    reason: str | None,
) -> None:
    """Flip `alerted=1` and record why the alert did NOT fire cleanly.

    `reason` is the value written to `signals.alert_skipped_reason`.
    For a successful live send pass `None` — the column is only
    meaningful for rows whose alert was queued, skipped, or failed.
    """
    conn.execute(
        """
        UPDATE signals
        SET alerted = 1,
            alert_skipped_reason = ?
        WHERE id = ?
        """,
        (reason, signal_id),
    )


def _insert_queued_notification(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    title: str,
    body: str,
    priority: str,
    tags: tuple[str, ...],
    created_at: datetime,
    bypass_quiet: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notifications (
            created_at, sent_at, symbol, signal_id,
            title, body, priority, tags,
            queued, bypass_quiet, delivered, delivery_attempts, last_error
        ) VALUES (
            ?, NULL, ?, ?,
            ?, ?, ?, ?,
            1, ?, 0, 0, NULL
        )
        """,
        (
            to_utc_iso(created_at),
            symbol,
            signal_id,
            title,
            body,
            priority,
            ",".join(tags),
            1 if bypass_quiet else 0,
        ),
    )
    return int(cur.lastrowid)


def _insert_delivered_notification(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    title: str,
    body: str,
    priority: str,
    tags: tuple[str, ...],
    created_at: datetime,
    sent_at: datetime,
    bypass_quiet: bool,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notifications (
            created_at, sent_at, symbol, signal_id,
            title, body, priority, tags,
            queued, bypass_quiet, delivered, delivery_attempts, last_error
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            0, ?, 1, 1, NULL
        )
        """,
        (
            to_utc_iso(created_at),
            to_utc_iso(sent_at),
            symbol,
            signal_id,
            title,
            body,
            priority,
            ",".join(tags),
            1 if bypass_quiet else 0,
        ),
    )
    return int(cur.lastrowid)


def _insert_failed_notification(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    symbol: str,
    title: str,
    body: str,
    priority: str,
    tags: tuple[str, ...],
    created_at: datetime,
    bypass_quiet: bool,
    last_error: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notifications (
            created_at, sent_at, symbol, signal_id,
            title, body, priority, tags,
            queued, bypass_quiet, delivered, delivery_attempts, last_error
        ) VALUES (
            ?, NULL, ?, ?,
            ?, ?, ?, ?,
            0, ?, 0, 1, ?
        )
        """,
        (
            to_utc_iso(created_at),
            symbol,
            signal_id,
            title,
            body,
            priority,
            ",".join(tags),
            1 if bypass_quiet else 0,
            last_error,
        ),
    )
    return int(cur.lastrowid)


def _format_message(
    *,
    symbol: str,
    severity: str,
    score: int,
    price: float,
    trigger_reason: str,
    dominant_tf: str | None,
    drop_pct: float | None,
    rsi_1h: float | None,
) -> tuple[str, str]:
    """Render a short (title, body) pair for a signal.

    Intentionally plain-text and compact — ntfy caps header size, and
    we want the user's phone lock screen to show something readable
    without expanding the notification. The title packs the most
    important decision-making context into a single glanceable line:

        BTCUSDT  STRONG 72  -25.9% 7d  @ 40.00

    The body carries the trigger reason plus RSI if we have it, so a
    user can tell at a glance why the signal fired.
    """
    sev_label = severity.replace("_", " ").upper()
    title_parts = [symbol, f"{sev_label} {score}"]
    if drop_pct is not None and dominant_tf:
        title_parts.append(f"-{drop_pct:.1f}% {dominant_tf}")
    title_parts.append(f"@ {price:g}")
    title = "  ".join(title_parts)

    body_parts = [trigger_reason]
    if rsi_1h is not None:
        body_parts.append(f"RSI1h {rsi_1h:.0f}")
    body = " | ".join(body_parts)
    return title, body
