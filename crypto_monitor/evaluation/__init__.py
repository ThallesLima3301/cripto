"""Signal and buy evaluation.

After a signal fires or a buy is recorded, the user eventually wants
to know: "did that pay off?" Block 8 answers that question in two
complementary pieces:

  * `signal_eval` — evaluates matured signals. For every signal row
    that is old enough (configurable via `MATURATION_DAYS`) and does
    not yet have a companion row in `signal_evaluations`, it
    computes 24h / 7d / 30d returns and max-gain/max-loss over the
    7-day window, assigns a verdict, and writes the row.

  * `buy_eval` — evaluates matured buys. Uses the agreed
    hourly-resolution intraday logic: the day's open (1h open of
    00:00 UTC), the day's hourly low (min of the 1h candle LOWS
    inside the buy's day), and 7d / 30d returns. Wording is kept
    technically accurate — this is NOT a tick-level low.

  * `verdict` — pure mapping from a return percent to a verdict
    label, driven by `EvaluationSettings` thresholds.

Maturation is intentionally strict: we only evaluate a signal or
buy after the full 30-day window has passed. That keeps each record
one-shot — inserted once, never updated — and lets the `UNIQUE`
constraint on `signal_id` / `buy_id` act as the idempotency guard
during reruns.
"""

from crypto_monitor.evaluation.buy_eval import (
    BuyEvalReport,
    BuyEvalResult,
    DayLowResult,
    compute_day_low_hourly,
    evaluate_buy,
    evaluate_pending_buys,
)
from crypto_monitor.evaluation.signal_eval import (
    SignalEvalReport,
    SignalEvalResult,
    evaluate_pending_signals,
    evaluate_signal,
)
from crypto_monitor.evaluation.verdict import (
    VERDICT_BAD,
    VERDICT_GOOD,
    VERDICT_GREAT,
    VERDICT_NEUTRAL,
    VERDICT_PENDING,
    VERDICT_POOR,
    assign_verdict,
)

__all__ = [
    # verdict
    "assign_verdict",
    "VERDICT_GREAT",
    "VERDICT_GOOD",
    "VERDICT_NEUTRAL",
    "VERDICT_POOR",
    "VERDICT_BAD",
    "VERDICT_PENDING",
    # signals
    "evaluate_signal",
    "evaluate_pending_signals",
    "SignalEvalResult",
    "SignalEvalReport",
    # buys
    "evaluate_buy",
    "evaluate_pending_buys",
    "BuyEvalResult",
    "BuyEvalReport",
    "DayLowResult",
    "compute_day_low_hourly",
]
