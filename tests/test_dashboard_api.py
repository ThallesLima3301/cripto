"""Tests for the dashboard read-only API (Step 1).

Two endpoints in scope:

  * ``/api/health``    — DB liveness + schema_version + freshness.
  * ``/api/overview``  — KPIs + regime + analytics digest + activity feed.

The tests drive the FastAPI app via ``fastapi.testclient.TestClient``
(which uses httpx under the hood — already installed transitively).
The DB connection dependency is overridden to point at the shared
``seed_conn`` fixture from ``conftest.py`` so the tests never touch
disk.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The dashboard layer is an optional extra (`pip install ".[dashboard]"`).
# When fastapi/pydantic aren't installed — for example in the scheduled
# `scan` GHA workflow, which only installs `requirements.txt` for the
# bot's runtime dependencies — pytest must SKIP this entire module
# instead of failing collection. Locally (and in any CI that installs
# the extra), all 12 tests run normally.
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from crypto_monitor.dashboard import api as dashboard_api  # noqa: E402
from crypto_monitor.dashboard.api import app  # noqa: E402
from crypto_monitor.dashboard.deps import get_db  # noqa: E402
from crypto_monitor.database.connection import get_connection  # noqa: E402
from crypto_monitor.database.migrations import run_migrations  # noqa: E402
from crypto_monitor.database.schema import init_db  # noqa: E402


UTC = timezone.utc


# ---------- fixtures ----------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A temp-file SQLite database with the full schema applied.

    A file (not ``:memory:``) is required because TestClient runs the
    request handler on a worker thread, and sqlite3 connections are
    thread-bound by default. The file lets the per-request dependency
    override open its own connection on the request's own thread —
    exactly how production behaves.
    """
    p = tmp_path / "dashboard.db"
    conn = get_connection(p)
    try:
        init_db(conn)
        run_migrations(conn)
    finally:
        conn.close()
    return p


@pytest.fixture
def seed_conn(db_path: Path):
    """A short-lived connection used by tests to seed rows."""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db_path: Path):
    """A TestClient that opens a fresh connection per request."""

    def _gen():
        conn = get_connection(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = _gen
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_signal(
    conn,
    *,
    symbol: str = "BTCUSDT",
    detected_at: datetime,
    severity: str = "strong",
    score: int = 72,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown
        ) VALUES (?, ?, ?, 100.0, ?, ?, 'test', 0, '{}')
        """,
        (symbol, _iso(detected_at), _iso(detected_at), score, severity),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_sell_signal(
    conn,
    *,
    buy_id: int,
    symbol: str = "BTCUSDT",
    detected_at: datetime,
    rule: str = "stop_loss",
    pnl_pct: float = -10.0,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO sell_signals (
            symbol, buy_id, detected_at, price_at_signal,
            rule_triggered, severity, reason, pnl_pct
        ) VALUES (?, ?, ?, 90.0, ?, 'high', 'test', ?)
        """,
        (symbol, buy_id, _iso(detected_at), rule, pnl_pct),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_buy(conn, *, symbol: str = "BTCUSDT") -> int:
    cur = conn.execute(
        """
        INSERT INTO buys (
            symbol, bought_at, price, amount_invested,
            quote_currency, quantity, signal_id, note, created_at
        ) VALUES (?, ?, 100.0, 1000.0, 'USDT', 10.0, NULL, NULL, ?)
        """,
        (symbol, "2026-04-20T00:00:00Z", "2026-04-20T00:00:00Z"),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_candle(
    conn,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1h",
    open_time: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO candles
            (symbol, interval, open_time, open, high, low, close,
             volume, close_time)
        VALUES (?, ?, ?, 100.0, 100.0, 100.0, 100.0, 100.0, ?)
        """,
        (
            symbol, interval,
            _iso(open_time),
            _iso(open_time + timedelta(hours=1)),
        ),
    )
    conn.commit()


def _insert_watching(conn, *, symbol: str = "BTCUSDT", score: int = 40) -> None:
    conn.execute(
        """
        INSERT INTO watchlist (
            symbol, status, first_seen_at, last_seen_at,
            last_score, expires_at
        ) VALUES (?, 'watching', ?, ?, ?, ?)
        """,
        (symbol, "2026-04-23T15:00:00Z", "2026-04-23T15:00:00Z",
         score, "2026-04-25T15:00:00Z"),
    )
    conn.commit()


# =====================================================================
# /api/health
# =====================================================================

class TestHealth:

    def test_health_ok_on_empty_db(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["status"] == "ok"
        # Migrations apply through Block 24+ -> at least version 4.
        assert body["data"]["schema_version"] >= 4
        # No candles yet → freshness indicator is null.
        assert body["data"]["latest_candle_close_at"] is None
        assert body["meta"] == {}

    def test_health_returns_latest_candle_close_at(self, client, seed_conn):
        # Seed two 1h candles; the helper must report the newest close.
        _insert_candle(
            seed_conn,
            open_time=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        )
        _insert_candle(
            seed_conn,
            open_time=datetime(2026, 4, 23, 15, 0, tzinfo=UTC),
        )
        resp = client.get("/api/health")
        assert resp.status_code == 200
        # close_time = open_time + 1h, so 16:00.
        assert resp.json()["data"]["latest_candle_close_at"] \
            == "2026-04-23T16:00:00Z"

    def test_health_503_when_db_is_locked(self, client, monkeypatch):
        """A locked DB surfaces as 503 (not 500) so the frontend can retry."""
        def _boom(_conn):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dashboard_api, "build_health", _boom)
        resp = client.get("/api/health")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


# =====================================================================
# /api/overview
# =====================================================================

class TestOverview:

    def test_overview_empty_db(self, client):
        resp = client.get("/api/overview")
        assert resp.status_code == 200
        body = resp.json()
        data = body["data"]
        assert data["signals_24h"] == 0
        assert data["signals_7d"] == 0
        assert data["sell_signals_7d"] == 0
        assert data["watchlist_active"] == 0
        assert data["open_buys"] == 0
        assert data["regime"] is None
        # Analytics on an empty input: total_signals=0 and every metric None.
        assert data["analytics"]["scope"] == "90d"
        assert data["analytics"]["total_signals"] == 0
        assert data["analytics"]["win_rate"] is None
        assert data["analytics"]["expectancy"] is None
        assert data["analytics"]["profit_factor"] is None
        assert data["recent_activity"] == []
        assert body["meta"]["analytics_scope"] == "90d"

    def test_overview_signal_counts_split_by_window(self, client, seed_conn):
        """signals_24h is a strict subset of signals_7d."""
        # Pin a "now" via FastAPI's natural clock by inserting rows
        # relative to wall-clock time. Within a fast test run the time
        # delta between insertion and the API call is sub-second.
        now = datetime.now(UTC)
        _insert_signal(seed_conn, detected_at=now - timedelta(hours=1))     # in 24h + 7d
        _insert_signal(seed_conn, detected_at=now - timedelta(days=2))      # not 24h, in 7d
        _insert_signal(seed_conn, detected_at=now - timedelta(days=10))     # not in either

        resp = client.get("/api/overview")
        data = resp.json()["data"]
        assert data["signals_24h"] == 1
        assert data["signals_7d"] == 2

    def test_overview_open_buys_excludes_sold(self, client, seed_conn):
        open_id = _insert_buy(seed_conn)
        sold_id = _insert_buy(seed_conn, symbol="ETHUSDT")
        seed_conn.execute(
            "UPDATE buys SET sold_at = ?, sold_price = 110.0 WHERE id = ?",
            ("2026-04-22T15:00:00Z", sold_id),
        )
        seed_conn.commit()

        resp = client.get("/api/overview")
        assert resp.json()["data"]["open_buys"] == 1
        # Sanity: the watcher count is independent of the buy state.
        assert resp.json()["data"]["watchlist_active"] == 0
        # Use the open id to silence "unused" lint hints; the test
        # above already asserted the count.
        assert open_id != sold_id

    def test_overview_watchlist_active_count(self, client, seed_conn):
        _insert_watching(seed_conn, symbol="BTCUSDT")
        _insert_watching(seed_conn, symbol="ETHUSDT")
        resp = client.get("/api/overview")
        assert resp.json()["data"]["watchlist_active"] == 2

    def test_overview_regime_serialized(self, client, seed_conn):
        seed_conn.execute(
            """
            INSERT INTO regime_snapshots
                (label, btc_ema_short, btc_ema_long,
                 btc_atr_14d, atr_percentile, determined_at)
            VALUES ('risk_on', 45000.0, 43000.0, 1200.0, 35.0,
                    '2026-04-23T12:00:00Z')
            """
        )
        seed_conn.commit()
        resp = client.get("/api/overview")
        regime = resp.json()["data"]["regime"]
        assert regime["label"] == "risk_on"
        assert regime["determined_at"] == "2026-04-23T12:00:00Z"
        assert regime["atr_percentile"] == 35.0

    def test_overview_recent_activity_merges_signals_and_sell(
        self, client, seed_conn,
    ):
        now = datetime.now(UTC)
        sig_id = _insert_signal(
            seed_conn, symbol="BTCUSDT",
            detected_at=now - timedelta(minutes=30),
            severity="strong", score=72,
        )
        buy_id = _insert_buy(seed_conn, symbol="ETHUSDT")
        sell_id = _insert_sell_signal(
            seed_conn, buy_id=buy_id, symbol="ETHUSDT",
            detected_at=now - timedelta(minutes=15),
            rule="stop_loss", pnl_pct=-10.5,
        )

        resp = client.get("/api/overview")
        feed = resp.json()["data"]["recent_activity"]
        # Two events, newest first (sell at -15m, signal at -30m).
        assert len(feed) == 2
        assert feed[0]["kind"] == "sell"
        assert feed[0]["id"] == sell_id
        assert feed[0]["symbol"] == "ETHUSDT"
        assert "stop_loss" in feed[0]["headline"]
        assert "-10.50%" in feed[0]["headline"]

        assert feed[1]["kind"] == "signal"
        assert feed[1]["id"] == sig_id
        assert feed[1]["symbol"] == "BTCUSDT"
        assert "strong" in feed[1]["headline"]
        assert "score=72" in feed[1]["headline"]

    def test_overview_503_when_db_locked(self, client, monkeypatch):
        def _boom(_conn):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dashboard_api, "build_overview", _boom)
        resp = client.get("/api/overview")
        assert resp.status_code == 503


# =====================================================================
# OpenAPI sanity
# =====================================================================

class TestOpenApiSurface:

    def test_openapi_lists_step1_routes(self, client):
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        assert "/api/health" in paths
        assert "/api/overview" in paths

    def test_response_envelope_shape(self, client):
        resp = client.get("/api/health")
        body = resp.json()
        # Every response carries `data` and `meta` — frontend's
        # fetch wrapper relies on this.
        assert "data" in body
        assert "meta" in body


# =====================================================================
# Step 2 — list / detail endpoints
# =====================================================================

def _seed_signal_evaluation(
    conn,
    *,
    signal_id: int,
    return_7d_pct: float = 5.0,
    verdict: str = "good",
) -> None:
    conn.execute(
        """
        INSERT INTO signal_evaluations (
            signal_id, evaluated_at, price_at_signal,
            return_7d_pct, max_gain_7d_pct, max_loss_7d_pct,
            time_to_mfe_hours, time_to_mae_hours, verdict
        ) VALUES (?, '2026-04-25T15:00:00Z', 100.0, ?, ?, ?, 24.0, 48.0, ?)
        """,
        (signal_id, return_7d_pct, return_7d_pct + 5.0,
         return_7d_pct - 5.0, verdict),
    )
    conn.commit()


def _insert_weekly_summary(
    conn,
    *,
    week_start: str,
    week_end: str,
    signal_count: int = 4,
    body: str = "weekly body",
    sent: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO weekly_summaries (
            week_start, week_end, generated_at, body,
            signal_count, buy_count,
            top_drop_symbol, top_drop_pct, sent
        ) VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, ?)
        """,
        (week_start, week_end, week_end, body, signal_count, sent),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------- /api/signals ----------

class TestSignalsList:

    def test_returns_empty_with_pagination_meta(self, client):
        resp = client.get("/api/signals")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["meta"] == {
            "total": 0, "limit": 50, "offset": 0, "next_offset": None,
        }

    def test_pagination_total_and_next_offset(self, client, seed_conn):
        # Seed 5 signals over a few minutes apart so detected_at orders cleanly.
        base = datetime.now(UTC)
        for i in range(5):
            _insert_signal(
                seed_conn, symbol="BTCUSDT",
                detected_at=base - timedelta(minutes=i),
                severity="strong", score=70 + i,
            )
        resp = client.get("/api/signals?limit=2&offset=0")
        body = resp.json()
        assert resp.status_code == 200
        assert len(body["data"]) == 2
        assert body["meta"]["total"] == 5
        assert body["meta"]["limit"] == 2
        assert body["meta"]["offset"] == 0
        assert body["meta"]["next_offset"] == 2

        resp_last = client.get("/api/signals?limit=2&offset=4")
        body_last = resp_last.json()
        assert len(body_last["data"]) == 1
        assert body_last["meta"]["next_offset"] is None

    def test_filters_by_symbol_and_severity(self, client, seed_conn):
        now = datetime.now(UTC)
        _insert_signal(seed_conn, symbol="BTCUSDT", detected_at=now,
                       severity="strong", score=70)
        _insert_signal(seed_conn, symbol="ETHUSDT", detected_at=now,
                       severity="strong", score=72)
        _insert_signal(seed_conn, symbol="BTCUSDT", detected_at=now,
                       severity="normal", score=55)

        resp = client.get("/api/signals?symbol=BTCUSDT")
        assert {r["symbol"] for r in resp.json()["data"]} == {"BTCUSDT"}
        assert resp.json()["meta"]["total"] == 2

        resp = client.get("/api/signals?severity=strong")
        assert {r["severity"] for r in resp.json()["data"]} == {"strong"}
        assert resp.json()["meta"]["total"] == 2

    def test_filters_by_from_to_window(self, client, seed_conn):
        old = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        recent = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        _insert_signal(seed_conn, symbol="BTCUSDT", detected_at=old)
        _insert_signal(seed_conn, symbol="BTCUSDT", detected_at=recent)

        resp = client.get(
            "/api/signals?from=2026-04-01T00:00:00Z&to=2026-05-01T00:00:00Z"
        )
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert rows[0]["detected_at"] == "2026-04-20T12:00:00Z"


class TestSignalDetail:

    def test_returns_404_for_unknown(self, client):
        resp = client.get("/api/signals/999999")
        assert resp.status_code == 404

    def test_returns_signal_without_evaluation_when_none(
        self, client, seed_conn,
    ):
        sig_id = _insert_signal(
            seed_conn, symbol="BTCUSDT",
            detected_at=datetime(2026, 4, 23, 15, 0, tzinfo=UTC),
            severity="strong", score=72,
        )
        resp = client.get(f"/api/signals/{sig_id}")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["id"] == sig_id
        assert body["evaluation"] is None
        assert body["score_breakdown"] == {}

    def test_includes_evaluation_block_when_available(
        self, client, seed_conn,
    ):
        sig_id = _insert_signal(
            seed_conn, symbol="BTCUSDT",
            detected_at=datetime(2026, 3, 23, 15, 0, tzinfo=UTC),
            severity="strong", score=72,
        )
        _seed_signal_evaluation(
            seed_conn, signal_id=sig_id,
            return_7d_pct=10.0, verdict="great",
        )
        body = client.get(f"/api/signals/{sig_id}").json()["data"]
        assert body["evaluation"] is not None
        assert body["evaluation"]["return_7d_pct"] == 10.0
        assert body["evaluation"]["verdict"] == "great"

    def test_score_breakdown_round_trips_json(self, client, seed_conn):
        # Hand-insert a row with a structured score_breakdown payload to
        # confirm the API parses + ships the JSON intact.
        seed_conn.execute(
            """
            INSERT INTO signals (
                symbol, detected_at, candle_hour, price_at_signal,
                score, severity, trigger_reason, reversal_signal,
                score_breakdown
            ) VALUES (?, ?, ?, 100.0, 72, 'strong', 't', 0, ?)
            """,
            (
                "BTCUSDT",
                "2026-04-23T15:00:00Z", "2026-04-23T15:00:00Z",
                '{"drop_magnitude": {"points": 25}, "trend_context": {"points": 5}}',
            ),
        )
        seed_conn.commit()
        sig_id = seed_conn.execute("SELECT MAX(id) AS m FROM signals").fetchone()["m"]

        body = client.get(f"/api/signals/{sig_id}").json()["data"]
        assert body["score_breakdown"]["drop_magnitude"]["points"] == 25
        assert body["score_breakdown"]["trend_context"]["points"] == 5


# ---------- /api/watchlist ----------

class TestWatchlist:

    def test_empty_returns_data_array(self, client):
        body = client.get("/api/watchlist").json()
        assert body["data"] == []

    def test_returns_active_rows(self, client, seed_conn):
        _insert_watching(seed_conn, symbol="BTCUSDT", score=40)
        _insert_watching(seed_conn, symbol="ETHUSDT", score=42)
        rows = client.get("/api/watchlist").json()["data"]
        assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}
        for r in rows:
            assert r["status"] == "watching"


# ---------- /api/open-buys ----------

class TestOpenBuys:

    def test_empty_when_no_open_buys(self, client):
        body = client.get("/api/open-buys").json()
        assert body["data"] == []

    def test_open_buy_with_watermark_and_current_price(
        self, client, seed_conn,
    ):
        buy_id = _insert_buy(seed_conn, symbol="BTCUSDT")
        # Seed a watermark and a recent 1h candle for "current price".
        seed_conn.execute(
            "INSERT INTO sell_tracking (symbol, buy_id, high_watermark, updated_at) "
            "VALUES ('BTCUSDT', ?, 130.0, '2026-04-23T15:00:00Z')",
            (buy_id,),
        )
        _insert_candle(
            seed_conn, symbol="BTCUSDT", interval="1h",
            open_time=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        )
        seed_conn.commit()

        rows = client.get("/api/open-buys").json()["data"]
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "BTCUSDT"
        assert row["high_watermark"] == 130.0
        # Buy price is 100; current = 100 (latest candle close) -> pnl = 0
        assert row["current_price"] == 100.0
        assert row["pnl_pct"] == 0.0
        # Drawdown from 130 to 100 ≈ -23.08%
        assert row["drawdown_from_high_pct"] is not None
        assert row["drawdown_from_high_pct"] < -20.0
        assert row["latest_close_at"] == "2026-04-23T15:00:00Z"

    def test_open_buy_without_candles_keeps_price_fields_null(
        self, client, seed_conn,
    ):
        _insert_buy(seed_conn, symbol="BTCUSDT")
        rows = client.get("/api/open-buys").json()["data"]
        assert len(rows) == 1
        assert rows[0]["current_price"] is None
        assert rows[0]["pnl_pct"] is None
        assert rows[0]["drawdown_from_high_pct"] is None


# ---------- /api/buys ----------

class TestBuysList:

    def test_status_open_excludes_sold_rows(self, client, seed_conn):
        open_id = _insert_buy(seed_conn, symbol="BTCUSDT")
        sold_id = _insert_buy(seed_conn, symbol="ETHUSDT")
        seed_conn.execute(
            "UPDATE buys SET sold_at = ?, sold_price = 110.0 WHERE id = ?",
            ("2026-04-22T15:00:00Z", sold_id),
        )
        seed_conn.commit()

        body = client.get("/api/buys?status=open").json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["id"] == open_id
        # sold_* columns project on every row, even open ones.
        assert body["data"][0]["sold_at"] is None

    def test_status_sold_includes_sold_at_price(self, client, seed_conn):
        b = _insert_buy(seed_conn, symbol="BTCUSDT")
        seed_conn.execute(
            "UPDATE buys SET sold_at = ?, sold_price = 110.0, "
            "sold_note = 'took profit' WHERE id = ?",
            ("2026-04-22T15:00:00Z", b),
        )
        seed_conn.commit()

        body = client.get("/api/buys?status=sold").json()
        assert body["meta"]["total"] == 1
        row = body["data"][0]
        assert row["sold_at"] == "2026-04-22T15:00:00Z"
        assert row["sold_price"] == 110.0
        assert row["sold_note"] == "took profit"

    def test_pagination_metadata(self, client, seed_conn):
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            _insert_buy(seed_conn, symbol=sym)
        body = client.get("/api/buys?limit=2").json()
        assert body["meta"]["total"] == 3
        assert body["meta"]["next_offset"] == 2

    def test_invalid_status_rejected_by_pattern(self, client):
        # FastAPI's Query(..., pattern=...) returns 422 for non-matching values.
        resp = client.get("/api/buys?status=lolwut")
        assert resp.status_code == 422


# ---------- /api/sell-signals ----------

class TestSellSignalsList:

    def test_empty(self, client):
        body = client.get("/api/sell-signals").json()
        assert body["data"] == []
        assert body["meta"]["total"] == 0

    def test_filter_by_rule(self, client, seed_conn):
        buy_id = _insert_buy(seed_conn, symbol="BTCUSDT")
        now = datetime.now(UTC)
        _insert_sell_signal(seed_conn, buy_id=buy_id, detected_at=now,
                            rule="stop_loss", pnl_pct=-12.0)
        _insert_sell_signal(seed_conn, buy_id=buy_id, detected_at=now,
                            rule="take_profit", pnl_pct=15.0)
        body = client.get("/api/sell-signals?rule=take_profit").json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["rule_triggered"] == "take_profit"

    def test_filter_by_symbol_and_window(self, client, seed_conn):
        b1 = _insert_buy(seed_conn, symbol="BTCUSDT")
        b2 = _insert_buy(seed_conn, symbol="ETHUSDT")
        old = datetime(2026, 3, 1, tzinfo=UTC)
        recent = datetime(2026, 4, 20, tzinfo=UTC)
        _insert_sell_signal(seed_conn, buy_id=b1, symbol="BTCUSDT",
                            detected_at=old)
        _insert_sell_signal(seed_conn, buy_id=b2, symbol="ETHUSDT",
                            detected_at=recent)
        body = client.get(
            "/api/sell-signals?symbol=ETHUSDT&from=2026-04-01T00:00:00Z"
        ).json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["symbol"] == "ETHUSDT"


# ---------- /api/analytics ----------

class TestAnalytics:

    def test_empty_returns_zero_total(self, client):
        body = client.get("/api/analytics?scope=all").json()
        assert body["meta"]["scope"] == "all"
        assert body["data"]["total_signals"] == 0
        assert body["data"]["overall"]["count"] == 0

    def test_aggregator_results_are_serialized(self, client, seed_conn):
        # Build 6 evaluated signals with mixed outcomes.
        for i, ret in enumerate((10.0, 12.0, -5.0, 8.0, -2.0, 15.0)):
            seed_conn.execute(
                """
                INSERT INTO signals (
                    symbol, detected_at, candle_hour, price_at_signal,
                    score, severity, trigger_reason, reversal_signal,
                    score_breakdown, dominant_trigger_timeframe,
                    regime_at_signal
                ) VALUES (?, ?, ?, 100.0, 70, 'strong', 't', 0, '{}',
                          '7d', 'neutral')
                """,
                (f"X{i}USDT",
                 f"2026-03-{20-i:02d}T12:00:00Z",
                 f"2026-03-{20-i:02d}T12:00:00Z"),
            )
            sig_id = int(
                seed_conn.execute("SELECT MAX(id) FROM signals").fetchone()[0]
            )
            _seed_signal_evaluation(
                seed_conn, signal_id=sig_id, return_7d_pct=ret,
            )

        body = client.get("/api/analytics?scope=all&min_signals=1").json()
        assert body["data"]["total_signals"] == 6
        assert body["data"]["overall"]["count"] == 6
        # 4 wins + 2 losses -> 66.67% win rate.
        assert body["data"]["overall"]["win_rate"] is not None
        assert 60 <= body["data"]["overall"]["win_rate"] <= 70

    def test_invalid_scope_rejected(self, client):
        resp = client.get("/api/analytics?scope=180d")
        assert resp.status_code == 422


# ---------- /api/weekly-summaries ----------

class TestWeeklySummariesList:

    def test_empty(self, client):
        body = client.get("/api/weekly-summaries").json()
        assert body["data"] == []
        assert body["meta"]["limit"] == 20

    def test_returns_newest_first(self, client, seed_conn):
        _insert_weekly_summary(
            seed_conn,
            week_start="2026-04-04T00:00:00Z",
            week_end="2026-04-11T00:00:00Z",
            body="older week",
        )
        _insert_weekly_summary(
            seed_conn,
            week_start="2026-04-11T00:00:00Z",
            week_end="2026-04-18T00:00:00Z",
            body="newer week",
        )
        rows = client.get("/api/weekly-summaries").json()["data"]
        assert rows[0]["body"] == "newer week"
        assert rows[1]["body"] == "older week"

    def test_limit_param_respected(self, client, seed_conn):
        for i in range(3):
            _insert_weekly_summary(
                seed_conn,
                week_start=f"2026-{i+1:02d}-01T00:00:00Z",
                week_end=f"2026-{i+1:02d}-08T00:00:00Z",
            )
        body = client.get("/api/weekly-summaries?limit=2").json()
        assert len(body["data"]) == 2
        assert body["meta"]["limit"] == 2


# ---------- /api/regime/* ----------

class TestRegimeEndpoints:

    def test_latest_returns_null_when_absent(self, client):
        body = client.get("/api/regime/latest").json()
        assert body["data"] is None

    def test_latest_returns_most_recent_snapshot(self, client, seed_conn):
        for label, ts in (
            ("risk_on", "2026-04-22T12:00:00Z"),
            ("risk_off", "2026-04-23T12:00:00Z"),
        ):
            seed_conn.execute(
                """
                INSERT INTO regime_snapshots
                    (label, btc_ema_short, btc_ema_long, btc_atr_14d,
                     atr_percentile, determined_at)
                VALUES (?, 45000.0, 43000.0, 1200.0, 35.0, ?)
                """,
                (label, ts),
            )
        seed_conn.commit()
        data = client.get("/api/regime/latest").json()["data"]
        assert data["label"] == "risk_off"
        assert data["determined_at"] == "2026-04-23T12:00:00Z"

    def test_history_respects_limit_and_orders_newest_first(
        self, client, seed_conn,
    ):
        for i, ts in enumerate((
            "2026-04-21T12:00:00Z",
            "2026-04-22T12:00:00Z",
            "2026-04-23T12:00:00Z",
        )):
            seed_conn.execute(
                """
                INSERT INTO regime_snapshots
                    (label, btc_ema_short, btc_ema_long, btc_atr_14d,
                     atr_percentile, determined_at)
                VALUES ('neutral', 1.0, 1.0, 1.0, ?, ?)
                """,
                (50.0 + i, ts),
            )
        seed_conn.commit()
        body = client.get("/api/regime/history?limit=2").json()
        assert len(body["data"]) == 2
        assert body["data"][0]["determined_at"] == "2026-04-23T12:00:00Z"
        assert body["data"][1]["determined_at"] == "2026-04-22T12:00:00Z"


# ---------- locked-DB → 503 on a Step-2 endpoint ----------

class TestStep2LockedDb:

    def test_signals_list_503_when_db_locked(self, client, monkeypatch):
        def _boom(_conn, **_kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(dashboard_api, "build_signals_page", _boom)
        resp = client.get("/api/signals")
        assert resp.status_code == 503


# ---------- OpenAPI surface (Step 2) ----------

class TestStep2OpenApi:

    def test_all_step2_routes_present(self, client):
        paths = client.get("/api/openapi.json").json()["paths"]
        for p in (
            "/api/signals",
            "/api/signals/{signal_id}",
            "/api/watchlist",
            "/api/open-buys",
            "/api/buys",
            "/api/sell-signals",
            "/api/analytics",
            "/api/weekly-summaries",
            "/api/regime/latest",
            "/api/regime/history",
        ):
            assert p in paths, f"missing path: {p}"
