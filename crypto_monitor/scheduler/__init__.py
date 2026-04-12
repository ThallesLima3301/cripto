"""Scheduler entrypoints.

Three thin orchestrators — `run_scan`, `run_weekly`, `run_maintenance`
— that wire together the lower-layer modules into the exact flow the
Windows Task Scheduler (or any other cron-like invoker) needs.

Each entrypoint does ONLY orchestration:

  * load settings (or accept an injected one)
  * open a DB connection (or accept an injected one)
  * ensure the schema exists (`init_db` is idempotent)
  * call the relevant lower-layer functions in order
  * collect a report for logging

All actual business logic (scoring, dedup, evaluations, retention,
alerting) lives in the dedicated modules — nothing here re-implements
those concerns.
"""

from crypto_monitor.scheduler.entrypoints import (
    MaintenanceReport,
    ScanReport,
    run_maintenance,
    run_scan,
    run_weekly,
)

__all__ = [
    "MaintenanceReport",
    "ScanReport",
    "run_maintenance",
    "run_scan",
    "run_weekly",
]
