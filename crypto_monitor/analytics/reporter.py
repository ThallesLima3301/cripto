"""Human-readable rendering for :class:`ExpectancyReport`.

Two surfaces:

  * :func:`format_expectancy_report`  — multiline CLI output. One
    section per slice, omits empty slicings, includes MFE / MAE /
    timing on the overall bucket. Designed for terminal width.
  * :func:`format_expectancy_summary` — compact single-line summary
    suitable for the ntfy weekly body. Falls back to a graceful
    "dados insuficientes" style line when there is nothing to report.

Both functions are pure: input dataclass in, string out. No DB,
no I/O. They never raise on empty / partial input.
"""

from __future__ import annotations

from crypto_monitor.analytics.aggregator import (
    SCORE_BUCKETS,
    ExpectancyBucket,
    ExpectancyReport,
)


# Sections rendered in this order in the detailed report. Keeping
# the order canonical means downstream tests can pin section
# headers without fighting dict-ordering.
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("by_severity",         "Por severidade"),
    ("by_regime",            "Por regime"),
    ("by_score_bucket",      "Por score"),
    ("by_dominant_trigger",  "Por gatilho dominante"),
)


# ---------- compact summary ----------

def format_expectancy_summary(report: ExpectancyReport) -> str:
    """Return a one-line summary for the weekly ntfy body.

    Examples
    --------
    Populated::

        "WR 55.5% · exp +2.3% · PF 1.85 — 42 sinais"

    Insufficient data::

        "Análise: dados insuficientes"
    """
    overall = report.overall
    if report.total_signals == 0 or overall.win_rate is None:
        return "Análise: dados insuficientes"

    parts: list[str] = [f"WR {overall.win_rate:.1f}%"]
    if overall.expectancy is not None:
        parts.append(f"exp {_signed_pct(overall.expectancy)}")
    if overall.profit_factor is not None:
        parts.append(f"PF {overall.profit_factor:.2f}")
    body = " · ".join(parts)
    return f"{body} — {overall.count} sinais"


# ---------- detailed report ----------

def format_expectancy_report(report: ExpectancyReport) -> str:
    """Render a multiline report for the CLI.

    Always returns at least a header. Empty / no-evaluable inputs are
    handled with a single "sem dados disponíveis" line so the CLI
    never produces an empty paragraph.
    """
    lines: list[str] = []
    lines.append(f"Analytics — {report.total_signals} sinais")
    lines.append("=" * 32)

    overall = report.overall
    if report.total_signals == 0 or overall.win_rate is None:
        lines.append("")
        lines.append("Sem dados disponíveis para análise.")
        return "\n".join(lines)

    lines.append("")
    lines.append("Geral")
    lines.extend(_overall_lines(overall))

    for attr, header in _SECTION_ORDER:
        buckets: dict[str, ExpectancyBucket] = getattr(report, attr)
        if not buckets:
            continue
        lines.append("")
        lines.append(header)
        # by_score_bucket gets rendered in the canonical order so
        # readers see "50-64" before "65-79". Other slicings are
        # rendered alphabetically.
        if attr == "by_score_bucket":
            keys = [b[0] for b in SCORE_BUCKETS if b[0] in buckets]
        else:
            keys = sorted(buckets.keys())
        for key in keys:
            lines.append(f"  {_bucket_one_line(key, buckets[key])}")

    return "\n".join(lines)


# ---------- helpers ----------

def _overall_lines(b: ExpectancyBucket) -> list[str]:
    out: list[str] = []
    out.append(f"  Win rate: {b.win_rate:.1f}%")
    if b.expectancy is not None:
        out.append(f"  Expectancy: {_signed_pct(b.expectancy)}")
    if b.profit_factor is not None:
        out.append(f"  Profit factor: {b.profit_factor:.2f}")
    if b.avg_win_pct is not None or b.avg_loss_pct is not None:
        win_part = (
            _signed_pct(b.avg_win_pct) if b.avg_win_pct is not None else "n/a"
        )
        loss_part = (
            _signed_pct(b.avg_loss_pct) if b.avg_loss_pct is not None else "n/a"
        )
        out.append(f"  Média ganho/perda: {win_part} / {loss_part}")
    if b.avg_mfe_pct is not None or b.avg_mae_pct is not None:
        mfe_part = (
            _signed_pct(b.avg_mfe_pct) if b.avg_mfe_pct is not None else "n/a"
        )
        mae_part = (
            _signed_pct(b.avg_mae_pct) if b.avg_mae_pct is not None else "n/a"
        )
        out.append(f"  MFE / MAE médios: {mfe_part} / {mae_part}")
    if (
        b.avg_time_to_mfe_hours is not None
        or b.avg_time_to_mae_hours is not None
    ):
        mfe_t = (
            f"{b.avg_time_to_mfe_hours:.1f}h"
            if b.avg_time_to_mfe_hours is not None else "n/a"
        )
        mae_t = (
            f"{b.avg_time_to_mae_hours:.1f}h"
            if b.avg_time_to_mae_hours is not None else "n/a"
        )
        out.append(f"  Tempo até pico fav/adv: {mfe_t} / {mae_t}")
    return out


def _bucket_one_line(key: str, b: ExpectancyBucket) -> str:
    """One-line bucket renderer for slicing sections."""
    parts: list[str] = []
    if b.win_rate is not None:
        parts.append(f"WR {b.win_rate:.1f}%")
    if b.expectancy is not None:
        parts.append(f"exp {_signed_pct(b.expectancy)}")
    if b.profit_factor is not None:
        parts.append(f"PF {b.profit_factor:.2f}")
    body = " · ".join(parts) if parts else "sem dados"
    return f"{key} ({b.count}): {body}"


def _signed_pct(value: float) -> str:
    """Format a signed percent (`+1.23%`, `-0.50%`, `0.00%`)."""
    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"
