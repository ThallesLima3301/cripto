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
