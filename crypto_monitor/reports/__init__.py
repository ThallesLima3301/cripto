"""Reports generated from stored signals, buys, and evaluations.

Block 9 ships the weekly summary only: count the signals fired,
find the biggest drop, count the buys logged, aggregate verdicts of
anything that matured during the window, render a compact body,
persist the row in `weekly_summaries`, and push it to ntfy.

The module is intentionally scoped narrow — no scheduling, no CLI,
no fancy charts. Those live in later blocks.
"""

from crypto_monitor.reports.weekly import (
    WeeklyRunResult,
    WeeklySummary,
    generate_and_send_weekly_summary,
    generate_weekly_summary,
    persist_weekly_summary,
    send_weekly_summary,
)

__all__ = [
    "WeeklySummary",
    "WeeklyRunResult",
    "generate_weekly_summary",
    "persist_weekly_summary",
    "send_weekly_summary",
    "generate_and_send_weekly_summary",
]
