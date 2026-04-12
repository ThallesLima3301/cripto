"""Tests for `crypto_monitor.notifications.policy.decide_alert`.

The policy function is pure — every test constructs the facts,
prior alert (if any), current time, and the alerts settings, then
asserts the returned AlertDecision. No DB, no wall clock.

The timezone used throughout is `America/Sao_Paulo` (UTC-3, no DST),
so the local hour is always `utc_hour - 3`. Quiet hours 22..8 local
means 01:00..11:00 UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crypto_monitor.notifications.policy import (
    ACTION_QUEUE,
    ACTION_SEND_NOW,
    ACTION_SKIP_COOLDOWN,
    PriorAlert,
    SignalFacts,
    decide_alert,
)


UTC = timezone.utc
TZ = "America/Sao_Paulo"


def _facts(**overrides) -> SignalFacts:
    defaults = dict(
        signal_id=1,
        symbol="BTCUSDT",
        severity="normal",
        score=60,
        candle_hour="2026-04-11T14:00:00Z",
    )
    defaults.update(overrides)
    return SignalFacts(**defaults)


# ---------- send-now path ----------

def test_fresh_signal_outside_quiet_hours_sends_now(alerts_settings):
    # 18:00 UTC = 15:00 local → outside 22..8 quiet hours.
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    decision = decide_alert(_facts(), None, now, alerts_settings, TZ)
    assert decision.action == ACTION_SEND_NOW
    assert decision.reason == "fresh"
    assert decision.override_cooldown is False
    assert decision.override_quiet_hours is False


# ---------- quiet-hours queue ----------

def test_normal_signal_inside_quiet_hours_is_queued(alerts_settings):
    # 04:00 UTC = 01:00 local → inside 22..8 quiet hours.
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    decision = decide_alert(_facts(severity="normal"), None, now, alerts_settings, TZ)
    assert decision.action == ACTION_QUEUE
    assert decision.reason == "quiet_hours"
    assert decision.override_quiet_hours is False


def test_strong_signal_inside_quiet_hours_is_queued(alerts_settings):
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    decision = decide_alert(
        _facts(severity="strong", score=70), None, now, alerts_settings, TZ
    )
    assert decision.action == ACTION_QUEUE


# ---------- very_strong bypasses quiet hours ----------

def test_very_strong_bypasses_quiet_hours(alerts_settings):
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    decision = decide_alert(
        _facts(severity="very_strong", score=85),
        None,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_SEND_NOW
    assert decision.override_quiet_hours is True
    assert decision.reason == "very_strong_bypass_quiet"


# ---------- cooldown skip ----------

def test_prior_inside_cooldown_without_escalation_jump_skips(alerts_settings):
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    # Prior alert 30 minutes ago, same-ish score (no jump).
    prior = PriorAlert(
        sent_at=now - timedelta(minutes=30),
        score=65,
        severity="strong",
    )
    decision = decide_alert(
        _facts(severity="strong", score=68),  # +3 < 10
        prior,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_SKIP_COOLDOWN
    assert decision.reason.startswith("cooldown:")


def test_prior_outside_cooldown_sends_normally(alerts_settings):
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    # Prior alert 3h ago — outside the 2h cooldown.
    prior = PriorAlert(
        sent_at=now - timedelta(minutes=180),
        score=65,
        severity="strong",
    )
    decision = decide_alert(
        _facts(severity="strong", score=68),
        prior,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_SEND_NOW
    assert decision.override_cooldown is False
    assert decision.reason == "fresh"


# ---------- escalation override ----------

def test_escalation_jump_overrides_cooldown(alerts_settings):
    now = datetime(2026, 4, 11, 18, 0, tzinfo=UTC)
    # Prior 30 min ago at score 60; new score 72 is +12 >= escalation_jump(10).
    prior = PriorAlert(
        sent_at=now - timedelta(minutes=30),
        score=60,
        severity="normal",
    )
    decision = decide_alert(
        _facts(severity="strong", score=72),
        prior,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_SEND_NOW
    assert decision.override_cooldown is True
    assert decision.reason == "escalation_override"


def test_escalation_override_still_queues_inside_quiet_hours(alerts_settings):
    # Strong signal overrides cooldown, but it's still inside quiet hours
    # and it's NOT very_strong — it must still be queued.
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)
    prior = PriorAlert(
        sent_at=now - timedelta(minutes=30),
        score=55,
        severity="normal",
    )
    decision = decide_alert(
        _facts(severity="strong", score=70),  # +15 >= 10
        prior,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_QUEUE
    assert decision.override_cooldown is True


def test_very_strong_with_prior_in_cooldown_bypasses_everything(alerts_settings):
    # Prior was a strong alert 10 min ago. A fresh very_strong must
    # bypass both cooldown (via escalation_jump) and quiet hours.
    now = datetime(2026, 4, 11, 4, 0, tzinfo=UTC)  # inside quiet hours
    prior = PriorAlert(
        sent_at=now - timedelta(minutes=10),
        score=70,
        severity="strong",
    )
    decision = decide_alert(
        _facts(severity="very_strong", score=85),  # +15
        prior,
        now,
        alerts_settings,
        TZ,
    )
    assert decision.action == ACTION_SEND_NOW
    assert decision.override_cooldown is True
    assert decision.override_quiet_hours is True
