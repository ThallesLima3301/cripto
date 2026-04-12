"""Weekly summary report.

A weekly summary is a single row written to `weekly_summaries` plus a
short ntfy notification. It answers the question "what happened last
week?" without making the user open the DB.

What goes in the summary
------------------------
Rolling 7-day window ending at `now` (half-open: start inclusive,
end exclusive). For that window:

  * count of signals fired, broken down by severity
  * the single biggest drop that triggered a signal (symbol + pct)
  * count of buys logged
  * count of signal/buy evaluations that matured this week, broken
    down by verdict

The body is a compact plain-text block ready for ntfy: short enough
to fit on a phone notification but structured enough to actually be
useful.

Design notes
------------
* `generate_weekly_summary` is read-only. It queries the DB and
  returns a `WeeklySummary` with both structured fields (for tests
  and downstream callers) and a rendered `body`.

* `persist_weekly_summary` is the write step. It mirrors only the
  columns that `weekly_summaries` actually has — richer fields
  (severity breakdown, verdict histogram) only live in the body.

* `send_weekly_summary` reuses the same injected-sender pattern
  Block 7 established, so tests can stub ntfy without touching HTTP.
  On success it flips `sent=1` so the next run does not double-send.

* `generate_and_send_weekly_summary` is the convenient orchestrator
  the scheduler (Block 10) will call.

Scope note
----------
Block 9 is NOT responsible for deciding *when* to run the weekly
summary — no scheduling, no "has a week already been generated"
guard beyond what the caller chooses to do. The scheduler block
will own that.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping

from crypto_monitor.config.settings import NtfySettings
from crypto_monitor.notifications.ntfy import SendResult, send_ntfy
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


logger = logging.getLogger(__name__)


WINDOW_DAYS = 7


# Order in which severities are listed in the body. Mirrors the
# ScoringSeverity ladder, strongest first so the interesting rows
# hit the user's eye immediately.
_SEVERITY_ORDER: tuple[str, ...] = ("very_strong", "strong", "normal")

# Order in which verdicts are listed in the body.
_VERDICT_ORDER: tuple[str, ...] = (
    "great", "good", "neutral", "poor", "bad", "pending",
)


NtfySender = Callable[..., SendResult]


@dataclass(frozen=True)
class WeeklySummary:
    """Structured weekly report + rendered ntfy body.

    `signal_by_severity` and `verdict_counts` are frozen by convention
    — the dataclass is frozen so the reference cannot be reassigned,
    and no code path inside this module mutates them after
    construction.
    """
    week_start: str  # inclusive, UTC ISO
    week_end: str    # exclusive, UTC ISO
    signal_count: int
    signal_by_severity: Mapping[str, int]
    buy_count: int
    top_drop_symbol: str | None
    top_drop_pct: float | None
    matured_count: int
    verdict_counts: Mapping[str, int]
    body: str


@dataclass(frozen=True)
class WeeklyRunResult:
    """Outcome of a `generate_and_send_weekly_summary` call."""
    summary: WeeklySummary
    summary_id: int
    send_result: SendResult


# ---------- public API ----------

def generate_weekly_summary(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
) -> WeeklySummary:
    """Build an in-memory `WeeklySummary` for the 7 days ending at `now`.

    Read-only: the database is not modified. Call
    `persist_weekly_summary` separately to write the row.
    """
    if now is None:
        now = now_utc()
    if window_days <= 0:
        raise ValueError("window_days must be positive")

    week_end_dt = now
    week_start_dt = now - timedelta(days=window_days)
    week_start_iso = to_utc_iso(week_start_dt)
    week_end_iso = to_utc_iso(week_end_dt)

    signal_by_severity = _count_signals_by_severity(
        conn, week_start_iso, week_end_iso
    )
    signal_count = sum(signal_by_severity.values())

    top_drop_symbol, top_drop_pct = _top_drop(
        conn, week_start_iso, week_end_iso
    )

    buy_count = _count_buys(conn, week_start_iso, week_end_iso)

    verdict_counts = _count_verdicts_matured_in_window(
        conn, week_start_iso, week_end_iso
    )
    matured_count = sum(verdict_counts.values())

    body = _render_body(
        week_start_iso=week_start_iso,
        week_end_iso=week_end_iso,
        signal_count=signal_count,
        signal_by_severity=signal_by_severity,
        top_drop_symbol=top_drop_symbol,
        top_drop_pct=top_drop_pct,
        buy_count=buy_count,
        matured_count=matured_count,
        verdict_counts=verdict_counts,
    )

    return WeeklySummary(
        week_start=week_start_iso,
        week_end=week_end_iso,
        signal_count=signal_count,
        signal_by_severity=signal_by_severity,
        buy_count=buy_count,
        top_drop_symbol=top_drop_symbol,
        top_drop_pct=top_drop_pct,
        matured_count=matured_count,
        verdict_counts=verdict_counts,
        body=body,
    )


def persist_weekly_summary(
    conn: sqlite3.Connection,
    summary: WeeklySummary,
    *,
    now: datetime | None = None,
) -> int:
    """Insert the summary into `weekly_summaries` with `sent=0`.

    Returns the new row id. Commits on success.
    """
    if now is None:
        now = now_utc()
    cur = conn.execute(
        """
        INSERT INTO weekly_summaries (
            week_start, week_end, generated_at, body,
            signal_count, buy_count,
            top_drop_symbol, top_drop_pct,
            sent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            summary.week_start,
            summary.week_end,
            to_utc_iso(now),
            summary.body,
            summary.signal_count,
            summary.buy_count,
            summary.top_drop_symbol,
            summary.top_drop_pct,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def send_weekly_summary(
    conn: sqlite3.Connection,
    summary_id: int,
    *,
    ntfy: NtfySettings,
    sender: NtfySender | None = None,
) -> SendResult:
    """Send a persisted weekly summary via ntfy.

    Looks up the row by id, builds a compact title from the window,
    and hands off to the sender. On success flips `sent=1`; on
    failure leaves the row untouched so a later retry can try again.
    """
    if sender is None:
        sender = send_ntfy

    row = conn.execute(
        """
        SELECT id, week_start, week_end, body, sent
        FROM weekly_summaries
        WHERE id = ?
        """,
        (summary_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"weekly_summary id={summary_id} not found")

    title = _render_title(row["week_start"], row["week_end"])

    result = sender(
        ntfy,
        title,
        row["body"],
        priority="default",
        tags=("weekly",),
    )
    if result.sent:
        conn.execute(
            "UPDATE weekly_summaries SET sent = 1 WHERE id = ?",
            (summary_id,),
        )
        conn.commit()
    return result


def generate_and_send_weekly_summary(
    conn: sqlite3.Connection,
    *,
    ntfy: NtfySettings,
    now: datetime | None = None,
    sender: NtfySender | None = None,
    window_days: int = WINDOW_DAYS,
) -> WeeklyRunResult:
    """Generate, persist, and send a weekly summary in one call.

    The scheduler (Block 10) will call this once per week. Failure
    to send does NOT roll back the persisted row — the caller can
    inspect `send_result.sent` and retry send by id later.
    """
    if now is None:
        now = now_utc()
    summary = generate_weekly_summary(
        conn, now=now, window_days=window_days
    )
    summary_id = persist_weekly_summary(conn, summary, now=now)
    send_result = send_weekly_summary(
        conn, summary_id, ntfy=ntfy, sender=sender
    )
    return WeeklyRunResult(
        summary=summary,
        summary_id=summary_id,
        send_result=send_result,
    )


# ---------- internals: queries ----------

def _count_signals_by_severity(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT severity, COUNT(*) AS cnt
        FROM signals
        WHERE detected_at >= ? AND detected_at < ?
        GROUP BY severity
        """,
        (start_iso, end_iso),
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["severity"]] = int(r["cnt"])
    return counts


def _top_drop(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> tuple[str | None, float | None]:
    """Return (symbol, drop_pct) of the biggest drop_trigger_pct in the window.

    `drop_trigger_pct` is stored as a positive magnitude (how far the
    price fell), so ORDER BY DESC picks the steepest drop. Ties are
    broken by earliest detection so the result is deterministic.
    """
    row = conn.execute(
        """
        SELECT symbol, drop_trigger_pct
        FROM signals
        WHERE detected_at >= ? AND detected_at < ?
          AND drop_trigger_pct IS NOT NULL
        ORDER BY drop_trigger_pct DESC, detected_at ASC, id ASC
        LIMIT 1
        """,
        (start_iso, end_iso),
    ).fetchone()
    if row is None:
        return (None, None)
    return (row["symbol"], float(row["drop_trigger_pct"]))


def _count_buys(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM buys
        WHERE bought_at >= ? AND bought_at < ?
        """,
        (start_iso, end_iso),
    ).fetchone()
    return int(row["cnt"]) if row is not None else 0


def _count_verdicts_matured_in_window(
    conn: sqlite3.Connection, start_iso: str, end_iso: str
) -> dict[str, int]:
    """Aggregate verdicts across signal_evaluations + buy_evaluations.

    "Matured this week" = an evaluation row whose `evaluated_at`
    falls inside the window. We merge both evaluation tables so the
    user sees a single verdict histogram.
    """
    counts: dict[str, int] = {}

    for table in ("signal_evaluations", "buy_evaluations"):
        rows = conn.execute(
            f"""
            SELECT verdict, COUNT(*) AS cnt
            FROM {table}
            WHERE evaluated_at >= ? AND evaluated_at < ?
              AND verdict IS NOT NULL
            GROUP BY verdict
            """,
            (start_iso, end_iso),
        ).fetchall()
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + int(r["cnt"])

    return counts


# ---------- internals: rendering ----------

def _render_title(week_start_iso: str, week_end_iso: str) -> str:
    start = week_start_iso[:10]
    end = week_end_iso[:10]
    return f"Crypto weekly  {start} -> {end}"


def _render_body(
    *,
    week_start_iso: str,
    week_end_iso: str,
    signal_count: int,
    signal_by_severity: Mapping[str, int],
    top_drop_symbol: str | None,
    top_drop_pct: float | None,
    buy_count: int,
    matured_count: int,
    verdict_counts: Mapping[str, int],
) -> str:
    """Produce a compact plain-text body for ntfy.

    Kept deterministic (no timestamps) so tests can assert exact
    substrings.
    """
    lines: list[str] = []
    lines.append("Crypto weekly report")
    lines.append(f"{week_start_iso[:10]} -> {week_end_iso[:10]} UTC")
    lines.append("")

    if signal_count == 0:
        lines.append("Signals: 0 (quiet week)")
    else:
        lines.append(f"Signals: {signal_count}")
        for sev in _SEVERITY_ORDER:
            cnt = signal_by_severity.get(sev, 0)
            if cnt:
                lines.append(f"  {sev}: {cnt}")
        if top_drop_symbol is not None and top_drop_pct is not None:
            lines.append(
                f"Top drop: {top_drop_symbol} -{top_drop_pct:.1f}%"
            )

    lines.append("")
    lines.append(f"Buys logged: {buy_count}")
    lines.append("")

    if matured_count == 0:
        lines.append("Matured this week: 0")
    else:
        lines.append(f"Matured this week: {matured_count}")
        for v in _VERDICT_ORDER:
            cnt = verdict_counts.get(v, 0)
            if cnt:
                lines.append(f"  {v}: {cnt}")

    return "\n".join(lines)
