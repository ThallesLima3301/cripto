"""Analytics aggregator (Block 25 — pure computation only).

Block 25 ships the on-demand math for slicing matured signal
evaluations into expectancy / win-rate / profit-factor / MFE / MAE
buckets. The aggregator is pure: it accepts a list of dicts and
returns dataclasses, with no DB access, no I/O, and no scheduler
coupling. Persistence (analytics snapshots), CLI surfacing, and
weekly report integration land in later blocks.
"""

from crypto_monitor.analytics.aggregator import (
    ExpectancyBucket,
    ExpectancyReport,
    SCORE_BUCKETS,
    compute_expectancy,
)
from crypto_monitor.analytics.loader import (
    ScopeName,
    load_evaluation_rows,
)
from crypto_monitor.analytics.reporter import (
    format_expectancy_report,
    format_expectancy_summary,
)

__all__ = [
    "ExpectancyBucket",
    "ExpectancyReport",
    "SCORE_BUCKETS",
    "ScopeName",
    "compute_expectancy",
    "format_expectancy_report",
    "format_expectancy_summary",
    "load_evaluation_rows",
]
