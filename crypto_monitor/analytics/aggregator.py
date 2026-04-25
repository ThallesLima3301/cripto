"""Pure expectancy / win-rate aggregator over signal evaluations.

The function :func:`compute_expectancy` walks a list of dicts shaped
like the ``signal_evaluations ⨝ signals`` join — each row carries
``severity``, ``regime_at_signal``, ``score``,
``dominant_trigger_timeframe`` plus the four return / peak / timing
fields shipped in Blocks 0 and 24.

Block 25 is pure. The function does not touch the database, does not
schedule anything, and does not write reports. Callers feed in rows
loaded by some other layer; the returned ``ExpectancyReport`` is a
plain dataclass safe to render, snapshot, or inspect in tests.

Conventions
-----------
* All percentages stay in the same units the upstream evaluation
  produced (``return_7d_pct``, ``max_gain_7d_pct``, ``max_loss_7d_pct``
  are signed percent values; ``win_rate`` is a 0–100 percentage).
* A row is a **win** when ``return_7d_pct > 0``, a **loss** when
  ``return_7d_pct < 0``, and break-even at exactly 0 (counted in
  ``count`` but contributing 0 to expectancy and ignored by
  ``profit_factor``).
* Rows with ``return_7d_pct is None`` (e.g., insufficient post-event
  candles) are still counted in ``count`` but are ignored by every
  return-derived metric. The MFE / MAE / timing averages use only
  rows where the relevant field is present.
* ``profit_factor`` is ``None`` when there were no losses — division
  by zero is a worse signal than a clear "n/a".
* Sliced buckets with fewer than ``min_signals`` rows are omitted
  from the report entirely. The ``overall`` bucket is always
  produced (with ``count=0`` and every metric ``None`` when the
  input is empty).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping


# Score buckets are documented as part of the public surface so
# downstream tooling can render them in the same canonical order
# without re-deriving the labels.
SCORE_BUCKETS: tuple[tuple[str, int, int], ...] = (
    # (label, lower_inclusive, upper_inclusive)
    ("50-64", 50, 64),
    ("65-79", 65, 79),
    ("80-100", 80, 100),
)


# ---------- output dataclasses ----------

@dataclass(frozen=True)
class ExpectancyBucket:
    """Per-bucket metrics. Every field is optional except ``count``.

    A field is ``None`` when its underlying input was empty (e.g.
    ``avg_win_pct`` is ``None`` when no rows in this bucket had a
    positive ``return_7d_pct``). This is deliberate: ``0`` would be
    indistinguishable from a real average of zero, which we never
    want to silently conflate.
    """
    count: int
    win_rate: float | None              # percent 0..100
    avg_win_pct: float | None
    avg_loss_pct: float | None
    expectancy: float | None            # percent
    profit_factor: float | None         # ratio (sum_wins / |sum_losses|)
    avg_mfe_pct: float | None
    avg_mae_pct: float | None
    avg_time_to_mfe_hours: float | None
    avg_time_to_mae_hours: float | None


@dataclass(frozen=True)
class ExpectancyReport:
    """Top-level analytics summary.

    ``total_signals`` is the count of rows passed in (regardless of
    whether the rows have evaluable returns). ``overall`` summarizes
    the entire input. The four ``by_*`` dicts slice the same input
    along one dimension each. Buckets below ``min_signals`` are
    omitted, so an absent key means "not enough data".
    """
    total_signals: int
    overall: ExpectancyBucket
    by_severity: dict[str, ExpectancyBucket]
    by_regime: dict[str, ExpectancyBucket]
    by_score_bucket: dict[str, ExpectancyBucket]
    by_dominant_trigger: dict[str, ExpectancyBucket]


# ---------- public API ----------

def compute_expectancy(
    evaluations: list[Mapping[str, Any]],
    *,
    min_signals: int = 5,
) -> ExpectancyReport:
    """Aggregate a list of evaluation dicts into an :class:`ExpectancyReport`.

    Parameters
    ----------
    evaluations
        Each item is a mapping with the fields listed at the module
        docstring. Missing keys are treated like ``None`` — useful so
        the caller can pass rows from any join shape without first
        normalizing every column.
    min_signals
        Minimum rows required for a sliced bucket to appear in the
        report. The ``overall`` bucket is unaffected by this filter.

    Returns
    -------
    ExpectancyReport
        Always non-None. An empty input produces a valid report with
        ``total_signals=0`` and an empty ``overall`` bucket.
    """
    overall = _bucket_from_rows(evaluations)

    by_severity = _group_buckets(
        evaluations, key="severity", min_signals=min_signals,
    )
    by_regime = _group_buckets(
        evaluations, key="regime_at_signal", min_signals=min_signals,
    )
    by_dominant_trigger = _group_buckets(
        evaluations, key="dominant_trigger_timeframe",
        min_signals=min_signals,
    )
    by_score_bucket = _group_score_buckets(
        evaluations, min_signals=min_signals,
    )

    return ExpectancyReport(
        total_signals=len(evaluations),
        overall=overall,
        by_severity=by_severity,
        by_regime=by_regime,
        by_score_bucket=by_score_bucket,
        by_dominant_trigger=by_dominant_trigger,
    )


# ---------- internals ----------

def _empty_bucket() -> ExpectancyBucket:
    return ExpectancyBucket(
        count=0,
        win_rate=None,
        avg_win_pct=None,
        avg_loss_pct=None,
        expectancy=None,
        profit_factor=None,
        avg_mfe_pct=None,
        avg_mae_pct=None,
        avg_time_to_mfe_hours=None,
        avg_time_to_mae_hours=None,
    )


def _bucket_from_rows(rows: list[Mapping[str, Any]]) -> ExpectancyBucket:
    """Compute one ``ExpectancyBucket`` from a flat list of rows."""
    count = len(rows)
    if count == 0:
        return _empty_bucket()

    returns = [
        float(r["return_7d_pct"])
        for r in rows
        if r.get("return_7d_pct") is not None
    ]
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]

    eval_n = len(returns)
    if eval_n == 0:
        win_rate = None
        avg_win = None
        avg_loss = None
        expectancy = None
        profit_factor = None
    else:
        win_rate = len(wins) / eval_n * 100.0
        avg_win = (sum(wins) / len(wins)) if wins else None
        avg_loss = (sum(losses) / len(losses)) if losses else None
        # Break-evens contribute 0 implicitly — we only multiply the
        # win and loss fractions by their respective averages.
        expectancy = 0.0
        if wins:
            expectancy += (len(wins) / eval_n) * avg_win
        if losses:
            expectancy += (len(losses) / eval_n) * avg_loss
        if losses:
            profit_factor = (sum(wins) / abs(sum(losses))) if wins else 0.0
        else:
            profit_factor = None

    return ExpectancyBucket(
        count=count,
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        expectancy=expectancy,
        profit_factor=profit_factor,
        avg_mfe_pct=_avg(rows, "max_gain_7d_pct"),
        avg_mae_pct=_avg(rows, "max_loss_7d_pct"),
        avg_time_to_mfe_hours=_avg(rows, "time_to_mfe_hours"),
        avg_time_to_mae_hours=_avg(rows, "time_to_mae_hours"),
    )


def _avg(rows: list[Mapping[str, Any]], key: str) -> float | None:
    """Average ``key`` across rows that have a non-None value, or None."""
    values = [float(r[key]) for r in rows if r.get(key) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _group_buckets(
    rows: list[Mapping[str, Any]],
    *,
    key: str,
    min_signals: int,
) -> dict[str, ExpectancyBucket]:
    """Slice rows by ``row[key]`` (None values are dropped)."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        value = r.get(key)
        if value is None:
            continue
        grouped[str(value)].append(r)
    return {
        k: _bucket_from_rows(v)
        for k, v in grouped.items()
        if len(v) >= min_signals
    }


def _group_score_buckets(
    rows: list[Mapping[str, Any]],
    *,
    min_signals: int,
) -> dict[str, ExpectancyBucket]:
    """Slice rows into the three documented score ranges.

    Rows whose score falls outside ``[50, 100]`` (or whose score is
    ``None``) are simply excluded from this slicing — they do not
    create a fourth bucket, mirroring the spec.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        score = r.get("score")
        if score is None:
            continue
        label = _score_bucket_label(int(score))
        if label is None:
            continue
        grouped[label].append(r)
    return {
        k: _bucket_from_rows(v)
        for k, v in grouped.items()
        if len(v) >= min_signals
    }


def _score_bucket_label(score: int) -> str | None:
    """Return the bucket label for ``score`` or ``None`` if out of range."""
    for label, lo, hi in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return None
