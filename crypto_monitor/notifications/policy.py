"""Pure alert-policy decision function.

`decide_alert` is a pure function: given a signal's facts, the prior
delivered alert for the same symbol (if any), the current time, the
alert config, and the target timezone, it returns an AlertDecision.

It does NOT:
  * read the database
  * read the wall clock
  * do I/O of any kind
  * recompute scoring

The orchestrator in `service.py` is responsible for loading these
inputs and executing the returned decision.

Decision ladder
---------------
1. **Cooldown gate** — if a prior alert exists for this symbol and
   was delivered within `cooldown_minutes`, we normally skip. The
   exception is the "escalation jump": if the new score is at least
   `escalation_jump` points above the prior alert's score, we allow
   the alert through and mark the decision with
   `override_cooldown=True`. This keeps a later, much stronger signal
   from being smothered by an earlier weak one.

2. **Quiet-hours gate** — if we're inside the configured local quiet
   hours, a `very_strong` signal bypasses quiet hours and sends now;
   anything else is queued for later delivery via `flush_queue`.

3. **Default** — send now.

`reason` on the AlertDecision is a short, machine-readable code that
the service layer writes to `signals.alert_skipped_reason` when the
decision is a skip, and to the notification's `last_error` slot when
debugging queued rows. It is intentionally small and stable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from crypto_monitor.config.settings import AlertSettings
from crypto_monitor.utils.time_utils import is_quiet_hours, minutes_between


# Action codes returned by `decide_alert`.
ACTION_SEND_NOW = "send_now"
ACTION_QUEUE = "queue"
ACTION_SKIP_COOLDOWN = "skip_cooldown"


@dataclass(frozen=True)
class SignalFacts:
    """Minimal view of a signal the policy layer needs to decide.

    Keeping this separate from `SignalCandidate` means the service
    layer can construct it from either an in-memory candidate or a
    row loaded from the `signals` table without coupling the policy
    to the full 30-field candidate type.
    """
    signal_id: int
    symbol: str
    severity: str            # "normal" | "strong" | "very_strong"
    score: int
    candle_hour: str


@dataclass(frozen=True)
class PriorAlert:
    """The most recent DELIVERED alert for a given symbol.

    `sent_at` must be timezone-aware UTC. The service layer filters
    rows by `delivered=1 AND sent_at IS NOT NULL` before building
    this, so queued-but-undelivered notifications do NOT count as
    a prior alert for cooldown purposes — a queued alert has not
    actually been shown to the user yet.
    """
    sent_at: datetime
    score: int
    severity: str


@dataclass(frozen=True)
class AlertDecision:
    """Outcome of `decide_alert`.

    `action` is one of the ACTION_* constants. `reason` is a short
    code suitable for logging and for the `alert_skipped_reason`
    column. The override flags are informational: the service layer
    uses them to populate the `bypass_quiet` column and for telemetry.
    """
    action: str
    reason: str
    override_cooldown: bool = False
    override_quiet_hours: bool = False


def decide_alert(
    signal: SignalFacts,
    prior: PriorAlert | None,
    now: datetime,
    alerts: AlertSettings,
    timezone_name: str,
) -> AlertDecision:
    """Decide what to do with a fresh signal. Pure function."""
    override_cooldown = False

    # 1. Cooldown gate.
    if prior is not None:
        elapsed = minutes_between(now, prior.sent_at)
        if elapsed < alerts.cooldown_minutes:
            jump = signal.score - prior.score
            if jump >= alerts.escalation_jump:
                # Escalation override: let the stronger signal through
                # even though we're still inside the cooldown window.
                override_cooldown = True
            else:
                return AlertDecision(
                    action=ACTION_SKIP_COOLDOWN,
                    reason=(
                        f"cooldown:{int(elapsed)}m<{alerts.cooldown_minutes}m"
                    ),
                )

    # 2. Quiet-hours gate.
    in_quiet = is_quiet_hours(
        now,
        timezone_name,
        alerts.quiet_hours_start,
        alerts.quiet_hours_end,
    )
    if in_quiet:
        if signal.severity == "very_strong":
            return AlertDecision(
                action=ACTION_SEND_NOW,
                reason="very_strong_bypass_quiet",
                override_cooldown=override_cooldown,
                override_quiet_hours=True,
            )
        return AlertDecision(
            action=ACTION_QUEUE,
            reason="quiet_hours",
            override_cooldown=override_cooldown,
        )

    # 3. Default path.
    reason = "escalation_override" if override_cooldown else "fresh"
    return AlertDecision(
        action=ACTION_SEND_NOW,
        reason=reason,
        override_cooldown=override_cooldown,
    )
