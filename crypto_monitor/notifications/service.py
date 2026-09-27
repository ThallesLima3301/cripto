"""Notification orchestrator.

This module walks unalerted rows in the `signals` table, asks
`policy.decide_alert` what to do with each, and executes the
decision:

  * `send_now`         — POST via `ntfy.send_ntfy`, then write a
                         notifications row with delivered=1 (on
                         success) or delivered=0+last_error (on
                         failure). Transient failures also set
                         queued=1 for a later scan. The signal is
                         marked `alerted=1` either way so processing
                         it again cannot create a second queue row.

  * `queue`            — write a notifications row with delivered=0,
                         queued=1, sent_at=NULL. These rows are the
                         pending-delivery queue and are picked up by
                         `flush_queue` once quiet hours end.

  * `skip_cooldown`    — do NOT write a notifications row; flip
                         `signals.alerted=1` with
                         `alert_skipped_reason=cooldown:...` so the
                         row does not get re-examined on the next scan.

`flush_queue` runs at the start of every scan. It reuses queued rows,
honors quiet hours except for very-strong alerts, and stops after five
delivery cycles or 24 hours from queue creation. Successful rows are
stamped with `sent_at`, `delivered=1`, `queued=0`. Only transient errors
remain queued; permanent, expired and exhausted failures retain their
diagnostic error as undelivered history. Historical queued=0 failures
are not automatically replayed.

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
from datetime import datetime, timedelta
from typing import Any, Callable

from crypto_monitor.config.settings import AlertSettings, NtfySettings
from crypto_monitor.notifications.formatters import (
    format_alert_body,
    format_alert_title,
)
from crypto_monitor.notifications.ntfy import SendResult, send_ntfy
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

# Bound delayed buy alerts so outages cannot trigger an unlimited
# replay of obsolete market signals. One attempt is one sender call;
# its HTTP retries are separately controlled by ntfy.max_retries.
MAX_DELIVERY_ATTEMPTS = 5
MAX_NOTIFICATION_AGE = timedelta(hours=24)


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
    """Run summary; queued includes failed sends deferred for retry.

    A transient failure contributes to both queued and send_failed.
    """
    considered: int
    sent: int
    queued: int
    skipped_cooldown: int
    send_failed: int


@dataclass(frozen=True)
class FlushReport:
    """Run summary; considered counts sends actually attempted.

    discarded counts rows removed undelivered, including failed
    terminal attempts as well as expiry without a send.
    """
    considered: int
    sent: int
    failed: int
    in_quiet_hours: bool
    discarded: int = 0


# ---------- main entry points ----------

def process_pending_signals(
    conn: sqlite3.Connection,
    *,
    alerts: AlertSettings,
    ntfy: NtfySettings,
    timezone_name: str,
    now: datetime | None = None,
    sender: NtfySender | None = None,
    sent_clock: Callable[[], datetime] | None = None,
) -> ProcessReport:
    """Walk unalerted signals and dispatch each via the alert policy.

    `now` and `sender` are injectable for testing. In production
    `now` defaults to `now_utc()` and `sender` defaults to
    `ntfy.send_ntfy`. `sent_clock` is sampled after a successful send
    to record server acknowledgement time. An explicit `now` freezes
    that clock too unless the caller supplies one (deterministic replay).
    """
    if sent_clock is None:
        sent_clock = now_utc if now is None else lambda: now
    if now is None:
        now = now_utc()
    if sender is None:
        sender = send_ntfy

    rows = conn.execute(
        """
        SELECT id, symbol, severity, score, candle_hour,
               price_at_signal, trigger_reason, detected_at,
               dominant_trigger_timeframe, drop_trigger_pct,
               rsi_1h, rsi_4h, rel_volume,
               drop_24h_pct, drop_7d_pct, drop_30d_pct,
               dist_support_pct, support_level_price,
               distance_from_30d_high_pct, distance_from_180d_high_pct,
               reversal_signal, score_breakdown,
               regime_at_signal
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

        debug = ntfy.debug_notifications
        signal_data = _row_to_dict(row)
        title = format_alert_title(
            facts.symbol, facts.severity, debug=debug,
        )
        body = format_alert_body(signal_data, debug=debug)
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
                sent_at=max(now, sent_clock()),
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
                # Eligibility persists even if the first attempt was
                # during the day and recovery happens at night.
                bypass_quiet=facts.severity == "very_strong",
                last_error=f"{result.reason}:{result.error or ''}",
                retryable=result.retryable,
            )
            _mark_signal_alerted(
                conn, facts.signal_id, f"send_failed:{result.reason}"
            )
            send_failed += 1
            queued += int(result.retryable)

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
    sent_clock: Callable[[], datetime] | None = None,
) -> FlushReport:
    """Retry queued buy alerts within their age and attempt limits.

    Returns a report including `in_quiet_hours` so the caller can
    distinguish "queue is empty" from "quiet hours are still active".
    Very-strong alerts retain their quiet-hours bypass on retry.
    """
    if sent_clock is None:
        sent_clock = now_utc if now is None else lambda: now
    if now is None:
        now = now_utc()
    if sender is None:
        sender = send_ntfy

    in_quiet = is_quiet_hours(
        now,
        timezone_name,
        alerts.quiet_hours_start,
        alerts.quiet_hours_end,
    )

    rows = conn.execute(
        """
        SELECT id, signal_id, symbol, title, body, priority, tags,
               created_at, delivery_attempts, bypass_quiet, last_error
        FROM notifications
        WHERE delivered = 0 AND queued = 1
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    considered = 0
    sent = 0
    failed = 0
    discarded = 0

    for row in rows:
        stop_reason = None
        if now - from_utc_iso(row["created_at"]) >= MAX_NOTIFICATION_AGE:
            stop_reason = "retry_expired"
        elif row["delivery_attempts"] >= MAX_DELIVERY_ATTEMPTS:
            stop_reason = "retry_exhausted"
        if stop_reason is not None:
            conn.execute(
                "UPDATE notifications SET queued = 0, last_error = ? WHERE id = ?",
                (f"{stop_reason}:{row['last_error'] or ''}", row["id"]),
            )
            discarded += 1
            continue
        if in_quiet and not row["bypass_quiet"]:
            continue

        considered += 1
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
                (to_utc_iso(max(now, sent_clock())), row["id"]),
            )
            conn.execute(
                """
                UPDATE signals SET alert_skipped_reason = NULL
                WHERE id = ? AND alert_skipped_reason LIKE 'send_failed:%'
                """,
                (row["signal_id"],),
            )
            sent += 1
        else:
            retry = result.retryable and (
                row["delivery_attempts"] + 1 < MAX_DELIVERY_ATTEMPTS
            )
            error = f"{result.reason}:{result.error or ''}"
            if result.retryable and not retry:
                error = f"retry_exhausted:{error}"
            conn.execute(
                """
                UPDATE notifications
                SET queued = ?,
                    delivery_attempts = delivery_attempts + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (int(retry), error, row["id"]),
            )
            failed += 1
            discarded += int(not retry)

    conn.commit()
    return FlushReport(
        considered=considered,
        sent=sent,
        failed=failed,
        in_quiet_hours=in_quiet,
        discarded=discarded,
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
    retryable: bool,
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
            ?, ?, 0, 1, ?
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
            int(retryable),
            1 if bypass_quiet else 0,
            last_error,
        ),
    )
    return int(cur.lastrowid)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, extracting reversal_pattern.

    The ``reversal_pattern`` field is stored inside the JSON
    ``score_breakdown`` column. We extract it here so the formatter
    has a flat dict to work with.
    """
    d: dict[str, Any] = dict(row)
    # Extract reversal pattern from score_breakdown JSON.
    breakdown_raw = d.get("score_breakdown")
    if breakdown_raw and isinstance(breakdown_raw, str):
        import json
        try:
            breakdown = json.loads(breakdown_raw)
            rev = breakdown.get("reversal_pattern", {})
            if isinstance(rev, dict):
                d["reversal_pattern"] = rev.get("pattern")
            else:
                d["reversal_pattern"] = None
        except (json.JSONDecodeError, TypeError):
            d["reversal_pattern"] = None
    else:
        d["reversal_pattern"] = None
    return d
