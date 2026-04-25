"""Block 26 — analytics reporter (compact + detailed) and loader.

The reporter is pure: it transforms ``ExpectancyReport`` into strings.
The loader is the only DB-facing piece in the analytics package — its
tests verify the join shape and the scope filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto_monitor.analytics import (
    ExpectancyBucket,
    ExpectancyReport,
    compute_expectancy,
    format_expectancy_report,
    format_expectancy_summary,
    load_evaluation_rows,
)


UTC = timezone.utc


# ---------- helpers ----------

def _row(
    *,
    return_pct: float | None,
    severity: str = "strong",
    regime: str | None = "neutral",
    score: int = 70,
    trigger: str | None = "7d",
) -> dict:
    return {
        "return_7d_pct": return_pct,
        "severity": severity,
        "regime_at_signal": regime,
        "score": score,
        "dominant_trigger_timeframe": trigger,
        "max_gain_7d_pct": (return_pct + 5.0) if return_pct is not None else None,
        "max_loss_7d_pct": (return_pct - 5.0) if return_pct is not None else None,
        "time_to_mfe_hours": 24.0,
        "time_to_mae_hours": 48.0,
    }


def _empty_report() -> ExpectancyReport:
    return compute_expectancy([], min_signals=1)


# ---------- format_expectancy_summary ----------

class TestFormatExpectancySummary:

    def test_empty_input_returns_insufficient_line(self):
        line = format_expectancy_summary(_empty_report())
        assert "dados insuficientes" in line.lower()

    def test_no_evaluable_returns_insufficient_line(self):
        rows = [_row(return_pct=None) for _ in range(3)]
        report = compute_expectancy(rows, min_signals=1)
        line = format_expectancy_summary(report)
        assert "dados insuficientes" in line.lower()

    def test_summary_includes_core_metrics(self):
        rows = [
            _row(return_pct=10.0),
            _row(return_pct=20.0),
            _row(return_pct=-5.0),
            _row(return_pct=-10.0),
        ]
        report = compute_expectancy(rows, min_signals=1)
        line = format_expectancy_summary(report)
        # "WR 50.0% · exp +3.75% · PF 2.00 — 4 sinais"
        assert "WR 50.0%" in line
        assert "exp +3.75%" in line
        assert "PF 2.00" in line
        assert "4 sinais" in line

    def test_summary_omits_pf_when_no_losses(self):
        rows = [_row(return_pct=v) for v in (5.0, 10.0, 7.0)]
        report = compute_expectancy(rows, min_signals=1)
        line = format_expectancy_summary(report)
        assert "WR 100.0%" in line
        assert "PF" not in line  # profit_factor is None
        assert "3 sinais" in line


# ---------- format_expectancy_report ----------

class TestFormatExpectancyReport:

    def test_empty_input_renders_header_and_no_data_message(self):
        out = format_expectancy_report(_empty_report())
        assert "Analytics — 0 sinais" in out
        assert "Sem dados disponíveis" in out

    def test_populated_report_includes_all_expected_sections(self):
        rows = []
        # 6 strong + 6 very_strong, mixed regime + score so every slice
        # passes the default min_signals=5.
        for i in range(6):
            rows.append(_row(
                return_pct=8.0 if i % 2 == 0 else -3.0,
                severity="strong", regime="risk_on",
                score=68, trigger="7d",
            ))
        for i in range(6):
            rows.append(_row(
                return_pct=12.0 if i % 2 == 0 else -2.0,
                severity="very_strong", regime="neutral",
                score=82, trigger="30d",
            ))
        report = compute_expectancy(rows, min_signals=5)
        out = format_expectancy_report(report)
        # Header + 12 signals total.
        assert "Analytics — 12 sinais" in out
        # Each section heading appears.
        assert "Geral" in out
        assert "Por severidade" in out
        assert "Por regime" in out
        assert "Por score" in out
        assert "Por gatilho dominante" in out
        # Bucket lines render in canonical score order.
        idx_65 = out.find("65-79 (")
        idx_80 = out.find("80-100 (")
        assert 0 < idx_65 < idx_80
        # MFE / MAE timing lines render on the overall section.
        assert "MFE / MAE" in out
        assert "Tempo até pico" in out

    def test_omits_empty_slicings(self):
        # Only 3 rows — none of the slicings hit min_signals=5.
        rows = [_row(return_pct=5.0) for _ in range(3)]
        report = compute_expectancy(rows, min_signals=5)
        out = format_expectancy_report(report)
        # The sliced section headers should NOT appear; only Geral does.
        assert "Geral" in out
        assert "Por severidade" not in out
        assert "Por regime" not in out
        assert "Por score" not in out
        assert "Por gatilho dominante" not in out


# ---------- load_evaluation_rows ----------

class TestLoadEvaluationRows:

    def _seed(
        self,
        memory_db,
        *,
        symbol: str = "BTCUSDT",
        detected_at: datetime,
        return_7d_pct: float = 5.0,
        score: int = 70,
        severity: str = "strong",
    ) -> int:
        cur = memory_db.execute(
            """
            INSERT INTO signals (
                symbol, detected_at, candle_hour, price_at_signal,
                score, severity, trigger_reason, reversal_signal,
                score_breakdown, dominant_trigger_timeframe,
                regime_at_signal
            ) VALUES (?, ?, ?, 100.0, ?, ?, 'test', 0, '{}', '7d', 'neutral')
            """,
            (
                symbol,
                detected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                detected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                score, severity,
            ),
        )
        signal_id = int(cur.lastrowid)
        memory_db.execute(
            """
            INSERT INTO signal_evaluations (
                signal_id, evaluated_at, price_at_signal,
                return_7d_pct, max_gain_7d_pct, max_loss_7d_pct,
                time_to_mfe_hours, time_to_mae_hours, verdict
            ) VALUES (?, ?, 100.0, ?, ?, ?, 24.0, 48.0, 'good')
            """,
            (
                signal_id,
                (detected_at + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                return_7d_pct,
                return_7d_pct + 5.0,
                return_7d_pct - 5.0,
            ),
        )
        memory_db.commit()
        return signal_id

    def test_scope_all_returns_every_evaluated_signal(self, memory_db):
        self._seed(memory_db, detected_at=datetime(2026, 1, 1, tzinfo=UTC))
        self._seed(memory_db, detected_at=datetime(2026, 4, 1, tzinfo=UTC),
                   symbol="ETHUSDT")
        rows = load_evaluation_rows(memory_db, scope="all")
        assert len(rows) == 2
        # Schema columns are projected into the join shape.
        assert {"score", "severity", "regime_at_signal",
                "dominant_trigger_timeframe", "return_7d_pct",
                "max_gain_7d_pct", "max_loss_7d_pct",
                "time_to_mfe_hours", "time_to_mae_hours"} <= rows[0].keys()

    def test_scope_30d_filters_by_signal_detected_at(self, memory_db):
        old = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        recent = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        self._seed(memory_db, detected_at=old)
        self._seed(memory_db, detected_at=recent, symbol="ETHUSDT")

        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)  # 13 days after recent
        rows = load_evaluation_rows(memory_db, scope="30d", now=now)
        symbols = {r["score"] for r in rows}  # crude shape sanity
        assert len(rows) == 1

    def test_scope_90d_window(self, memory_db):
        old = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)         # 113d before now
        mid = datetime(2026, 2, 14, 12, 0, tzinfo=UTC)        # ~68d before now
        recent = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
        self._seed(memory_db, detected_at=old, symbol="OLD")
        self._seed(memory_db, detected_at=mid, symbol="MID")
        self._seed(memory_db, detected_at=recent, symbol="REC")

        now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
        rows = load_evaluation_rows(memory_db, scope="90d", now=now)
        # Old is outside 90d, mid + recent are inside.
        assert len(rows) == 2

    def test_unknown_scope_raises(self, memory_db):
        with pytest.raises(ValueError, match="unsupported scope"):
            load_evaluation_rows(memory_db, scope="180d")  # type: ignore[arg-type]
