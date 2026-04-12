"""Notification sending + alert policy + quiet-hours queue.

The three pieces are deliberately separated so each is easy to test:

  * `ntfy`    — a pure HTTP sender. Given an NtfySettings and a title/
                body, it POSTs to the ntfy server and returns a
                SendResult. No DB, no policy. NTFY_TOPIC is validated
                here (at send time) rather than at config-load time.

  * `policy`  — a pure decision function. Given a SignalFacts, the
                prior alert for the same symbol, the current time, and
                the relevant slices of config, it returns an
                AlertDecision. No DB, no wall clock, no I/O.

  * `service` — the orchestrator that walks unalerted signal rows,
                asks `policy` what to do, and executes the decision
                via `ntfy` + writes to the DB. Also contains
                `flush_queue`, which drains queued notifications once
                quiet hours end.

Block 7 does NOT ship a "scan" entry point — wiring ingestion +
scoring + notification together is the Block 10 scheduler's job.
"""

from crypto_monitor.notifications.ntfy import (
    REASON_HTTP_ERROR,
    REASON_MISSING_TOPIC,
    REASON_NETWORK_ERROR,
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
from crypto_monitor.notifications.service import (
    FlushReport,
    ProcessReport,
    flush_queue,
    process_pending_signals,
)

__all__ = [
    # ntfy
    "SendResult",
    "send_ntfy",
    "REASON_SENT",
    "REASON_MISSING_TOPIC",
    "REASON_HTTP_ERROR",
    "REASON_NETWORK_ERROR",
    # policy
    "SignalFacts",
    "PriorAlert",
    "AlertDecision",
    "decide_alert",
    "ACTION_SEND_NOW",
    "ACTION_QUEUE",
    "ACTION_SKIP_COOLDOWN",
    # service
    "ProcessReport",
    "FlushReport",
    "process_pending_signals",
    "flush_queue",
]
