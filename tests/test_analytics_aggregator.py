"""Block 25 — pure analytics aggregator.

The aggregator is data-in, data-out. Tests feed in lists of dicts
shaped like the ``signal_evaluations ⨝ signals`` join and pin every
metric on the resulting :class:`ExpectancyReport`.

Coverage:
  * basic expectancy math — wins, losses, profit factor, expectancy
  * MFE / MAE / timing averaging
  * slicing by severity / regime / score bucket / dominant trigger
  * all-winners and all-losers paths
  * small-bucket omission honors ``min_signals``
  * empty input → valid empty report
  * missing fields (None) handled cleanly
"""

from __future__ import annotations

import pytest

from crypto_monitor.analytics import (
    ExpectancyBucket,
    ExpectancyReport,
    SCORE_BUCKETS,
    compute_expectancy,
)


# ---------- helpers ----------

def _row(
    *,
    return_pct: float | None,
    severity: str = "strong",
    regime: str | None = "neutral",
    score: int = 70,
    trigger: str | None = "7d",
    mfe: float | None = None,
    mae: float | None = None,
    t_mfe: float | None = None,
    t_mae: float | None = None,
) -> dict:
    """Build one evaluation row. Field names mirror the SQL columns."""
    return {
        "return_7d_pct": return_pct,
        "severity": severity,
        "regime_at_signal": regime,
        "score": score,
        "dominant_trigger_timeframe": trigger,
        "max_gain_7d_pct": mfe,
        "max_loss_7d_pct": mae,
        "time_to_mfe_hours": t_mfe,
        "time_to_mae_hours": t_mae,
    }


# ---------- empty input ----------

class TestEmptyReport:

    def test_empty_input_returns_valid_empty_report(self):
        report = compute_expectancy([])
        assert isinstance(report, ExpectancyReport)
        assert report.total_signals == 0
        assert report.overall.count == 0
        # Every metric on the empty bucket is None.
        for field in (
            "win_rate", "avg_win_pct", "avg_loss_pct",
            "expectancy", "profit_factor",
            "avg_mfe_pct", "avg_mae_pct",
            "avg_time_to_mfe_hours", "avg_time_to_mae_hours",
        ):
            assert getattr(report.overall, field) is None
        # All slicings empty.
        assert report.by_severity == {}
        assert report.by_regime == {}
        assert report.by_score_bucket == {}
        assert report.by_dominant_trigger == {}


# ---------- basic math ----------

class TestBasicExpectancyMath:

    def test_win_rate_and_expectancy_basic(self):
        rows = [
            _row(return_pct=10.0),  # win
            _row(return_pct=-5.0),  # loss
            _row(return_pct=20.0),  # win
            _row(return_pct=-10.0), # loss
        ]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.count == 4
        # 2 wins out of 4 = 50%
        assert b.win_rate == pytest.approx(50.0)
        assert b.avg_win_pct == pytest.approx(15.0)   # (10 + 20) / 2
        assert b.avg_loss_pct == pytest.approx(-7.5)  # (-5 + -10) / 2
        # expectancy = 0.5 * 15 + 0.5 * -7.5 = 3.75
        assert b.expectancy == pytest.approx(3.75)
        # profit_factor = (10 + 20) / |(-5 + -10)| = 30 / 15 = 2.0
        assert b.profit_factor == pytest.approx(2.0)

    def test_break_even_does_not_count_as_win_or_loss(self):
        rows = [
            _row(return_pct=10.0),
            _row(return_pct=0.0),    # break-even
            _row(return_pct=-5.0),
        ]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.count == 3
        # 1 win / 3 evaluable = 33.33%
        assert b.win_rate == pytest.approx(100.0 / 3.0)
        # expectancy = (1/3)*10 + (1/3)*-5 = ~1.667
        assert b.expectancy == pytest.approx(5.0 / 3.0)

    def test_none_returns_excluded_from_evaluable_metrics(self):
        rows = [
            _row(return_pct=10.0),
            _row(return_pct=None),   # ignored by win-rate / expectancy
            _row(return_pct=-5.0),
        ]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.count == 3
        # 1 win out of 2 evaluable rows
        assert b.win_rate == pytest.approx(50.0)
        # expectancy = 0.5 * 10 + 0.5 * -5 = 2.5
        assert b.expectancy == pytest.approx(2.5)

    def test_mfe_mae_timing_averages(self):
        rows = [
            _row(return_pct=5.0, mfe=10.0, mae=-3.0, t_mfe=24.0, t_mae=72.0),
            _row(return_pct=-2.0, mfe=4.0, mae=-8.0, t_mfe=12.0, t_mae=48.0),
            _row(return_pct=8.0, mfe=12.0, mae=None, t_mfe=None, t_mae=24.0),
        ]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        # avg_mfe = (10+4+12)/3
        assert b.avg_mfe_pct == pytest.approx(26.0 / 3.0)
        # avg_mae averages only the two non-None rows
        assert b.avg_mae_pct == pytest.approx(-5.5)
        # t_mfe averages only the two non-None rows
        assert b.avg_time_to_mfe_hours == pytest.approx(18.0)
        # t_mae averages all three
        assert b.avg_time_to_mae_hours == pytest.approx(48.0)


# ---------- all winners / all losers ----------

class TestExtremeCases:

    def test_all_winners_profit_factor_is_none(self):
        rows = [_row(return_pct=v) for v in (5.0, 10.0, 7.0)]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.win_rate == pytest.approx(100.0)
        assert b.profit_factor is None  # no losses
        assert b.avg_loss_pct is None
        assert b.expectancy == pytest.approx((5 + 10 + 7) / 3.0)

    def test_all_losers_profit_factor_is_zero(self):
        rows = [_row(return_pct=v) for v in (-3.0, -5.0, -1.0)]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.win_rate == pytest.approx(0.0)
        # No wins, but losses exist -> profit_factor = 0 / sum(losses) = 0.0
        assert b.profit_factor == pytest.approx(0.0)
        assert b.avg_win_pct is None
        assert b.expectancy == pytest.approx((-3 + -5 + -1) / 3.0)

    def test_all_break_even_zero_expectancy(self):
        rows = [_row(return_pct=0.0) for _ in range(3)]
        report = compute_expectancy(rows, min_signals=1)
        b = report.overall
        assert b.win_rate == pytest.approx(0.0)
        assert b.profit_factor is None  # no losses, no wins
        assert b.expectancy == pytest.approx(0.0)


# ---------- slicing ----------

class TestBySeverity:

    def test_severity_buckets_split_correctly(self):
        rows = [
            _row(return_pct=10.0, severity="strong"),
            _row(return_pct=20.0, severity="strong"),
            _row(return_pct=-5.0, severity="strong"),
            _row(return_pct=15.0, severity="very_strong"),
            _row(return_pct=8.0, severity="very_strong"),
        ]
        report = compute_expectancy(rows, min_signals=2)
        assert set(report.by_severity.keys()) == {"strong", "very_strong"}
        strong = report.by_severity["strong"]
        vs = report.by_severity["very_strong"]
        assert strong.count == 3
        assert vs.count == 2
        assert vs.win_rate == pytest.approx(100.0)


class TestByRegime:

    def test_regime_buckets_drop_none_label(self):
        rows = [
            _row(return_pct=10.0, regime="risk_on"),
            _row(return_pct=-3.0, regime="risk_on"),
            _row(return_pct=5.0, regime="risk_off"),
            _row(return_pct=-1.0, regime=None),  # excluded from regime slicing
        ]
        report = compute_expectancy(rows, min_signals=1)
        assert "risk_on" in report.by_regime
        assert "risk_off" in report.by_regime
        assert "None" not in report.by_regime
        # Overall still counts all 4 rows.
        assert report.overall.count == 4
        # risk_on aggregates the two risk_on rows.
        assert report.by_regime["risk_on"].count == 2


class TestByScoreBucket:

    def test_score_bucket_labels_match_constant(self):
        labels = {b[0] for b in SCORE_BUCKETS}
        assert labels == {"50-64", "65-79", "80-100"}

    def test_score_buckets_split_on_documented_ranges(self):
        rows = [
            _row(return_pct=5.0, score=50),    # 50-64
            _row(return_pct=8.0, score=64),    # 50-64
            _row(return_pct=12.0, score=65),   # 65-79
            _row(return_pct=14.0, score=79),   # 65-79
            _row(return_pct=20.0, score=80),   # 80-100
            _row(return_pct=22.0, score=100),  # 80-100
            _row(return_pct=2.0, score=49),    # excluded (out of range)
            _row(return_pct=2.0, score=101),   # excluded (out of range)
        ]
        report = compute_expectancy(rows, min_signals=1)
        assert set(report.by_score_bucket.keys()) == {"50-64", "65-79", "80-100"}
        assert report.by_score_bucket["50-64"].count == 2
        assert report.by_score_bucket["65-79"].count == 2
        assert report.by_score_bucket["80-100"].count == 2
        # Overall still counts every row.
        assert report.overall.count == 8

    def test_score_bucket_skips_none_score(self):
        rows = [
            _row(return_pct=5.0, score=70),
            _row(return_pct=8.0, score=None),  # ignored by score slicing
        ]
        report = compute_expectancy(rows, min_signals=1)
        assert "65-79" in report.by_score_bucket
        assert report.by_score_bucket["65-79"].count == 1


class TestByDominantTrigger:

    def test_dominant_trigger_buckets_split_and_drop_none(self):
        rows = [
            _row(return_pct=5.0, trigger="1h"),
            _row(return_pct=-2.0, trigger="1h"),
            _row(return_pct=12.0, trigger="7d"),
            _row(return_pct=4.0, trigger=None),  # excluded
        ]
        report = compute_expectancy(rows, min_signals=1)
        assert set(report.by_dominant_trigger.keys()) == {"1h", "7d"}
        assert report.by_dominant_trigger["1h"].count == 2
        assert report.by_dominant_trigger["7d"].count == 1


# ---------- min_signals filter ----------

class TestMinSignalsFilter:

    def test_buckets_below_min_signals_are_omitted(self):
        rows = [
            _row(return_pct=5.0, severity="strong"),
            _row(return_pct=8.0, severity="strong"),
            _row(return_pct=12.0, severity="strong"),
            _row(return_pct=20.0, severity="very_strong"),  # only 1 row
        ]
        report = compute_expectancy(rows, min_signals=3)
        assert "strong" in report.by_severity
        assert "very_strong" not in report.by_severity

    def test_overall_bucket_ignores_min_signals(self):
        """The overall bucket is always populated from every row."""
        rows = [_row(return_pct=5.0)]
        report = compute_expectancy(rows, min_signals=10)
        assert report.overall.count == 1
        assert report.overall.win_rate == pytest.approx(100.0)

    def test_default_min_signals_is_five(self):
        rows = [
            _row(return_pct=5.0, severity="normal") for _ in range(4)
        ]
        report = compute_expectancy(rows)  # default min_signals=5
        assert "normal" not in report.by_severity

        rows.append(_row(return_pct=5.0, severity="normal"))
        report2 = compute_expectancy(rows)
        assert "normal" in report2.by_severity


# ---------- robustness ----------

class TestRobustness:

    def test_missing_keys_do_not_crash(self):
        """Rows that omit some columns should still aggregate cleanly."""
        rows = [
            {"return_7d_pct": 5.0},   # nothing else
            {"return_7d_pct": -2.0},
        ]
        report = compute_expectancy(rows, min_signals=1)
        # Overall computes from returns; slicings see no labels and stay empty.
        assert report.overall.count == 2
        assert report.by_severity == {}
        assert report.by_regime == {}
        assert report.by_score_bucket == {}
        assert report.by_dominant_trigger == {}

    def test_returns_are_isolated_per_bucket(self):
        """Slicing is structural, not statistical — each bucket sums its own rows."""
        rows = [
            _row(return_pct=10.0, severity="strong"),
            _row(return_pct=-10.0, severity="strong"),
            _row(return_pct=20.0, severity="very_strong"),
        ]
        report = compute_expectancy(rows, min_signals=1)
        # very_strong has only the +20 row, so its win_rate = 100% and PF=None.
        vs = report.by_severity["very_strong"]
        assert vs.win_rate == pytest.approx(100.0)
        assert vs.profit_factor is None
        # strong has 50/50 with PF = 10 / 10 = 1.0
        s = report.by_severity["strong"]
        assert s.win_rate == pytest.approx(50.0)
        assert s.profit_factor == pytest.approx(1.0)


class TestExpectancyBucketDataclass:

    def test_bucket_is_frozen(self):
        b = ExpectancyBucket(
            count=1, win_rate=100.0,
            avg_win_pct=5.0, avg_loss_pct=None,
            expectancy=5.0, profit_factor=None,
            avg_mfe_pct=None, avg_mae_pct=None,
            avg_time_to_mfe_hours=None, avg_time_to_mae_hours=None,
        )
        with pytest.raises(Exception):
            b.count = 2  # type: ignore[misc]
