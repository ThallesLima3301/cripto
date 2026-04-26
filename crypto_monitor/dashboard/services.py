"""Per-endpoint service functions.

A "service" here is a thin function that takes a ``Connection`` (and
sometimes a ``now`` clock) and returns the schema model for one
endpoint. All composition lives here so the route module stays a
pure HTTP shell.

Rules of this layer:

  * Never write SQL inline. If a query is needed and no reader
    covers it, add the reader to the appropriate ``*/store.py``
    first.
  * Never import from FastAPI. Services are plain Python and
    independently testable.
  * Tolerate empty / missing data. The bot is local-first and a
    fresh install will hit every "no rows" path on day one — the
    schemas surface that as ``None`` / ``0``, not as exceptions.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from crypto_monitor.analytics import (
    compute_expectancy,
    load_evaluation_rows,
)
from crypto_monitor.dashboard.schemas import (
    ActivityItem,
    HealthData,
    OverviewAnalytics,
    OverviewData,
    OverviewRegime,
)
from crypto_monitor.database.schema import get_schema_version
from crypto_monitor.regime.store import load_latest_regime
from crypto_monitor.sell.store import (
    count_sell_signals_since,
    list_recent_sell_signals,
    load_open_buys,
)
from crypto_monitor.signals.persistence import (
    count_signals_since,
    latest_candle_close_time,
    list_recent_signals,
)
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso
from crypto_monitor.watchlist import list_watching


logger = logging.getLogger(__name__)


# Recent-activity feed merges the latest N signals + sell signals;
# the constant lives here so the frontend can never accidentally
# request a different number than the backend actually produces.
_ACTIVITY_LIMIT = 10


# ---------- /api/health ----------

def build_health(conn: sqlite3.Connection) -> HealthData:
    """Health probe service.

    Touches the DB enough to confirm the connection is working
    (``get_schema_version`` reads the ``schema_meta`` table) and
    grabs the latest 1h candle close as a freshness indicator.

    Block 24+ migrations should always be applied by the time this
    endpoint is hit — the bot's own scheduler entrypoints run
    ``init_db`` + ``run_migrations`` at startup. We intentionally
    do **not** run migrations here: the API is read-only, and a
    server that quietly schema-bumps on first GET would surprise
    operators who expect schema changes only via the bot's own
    pipeline.
    """
    schema_version = get_schema_version(conn)
    latest_close = latest_candle_close_time(conn, interval="1h")
    return HealthData(
        status="ok",
        schema_version=schema_version,
        latest_candle_close_at=latest_close,
    )


# ---------- /api/overview ----------

def build_overview(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> OverviewData:
    """Compose the home page's KPI strip + activity feed.

    ``now`` is injectable so tests can pin a deterministic clock; in
    production it defaults to ``now_utc()``.
    """
    now = now or now_utc()
    since_24h_iso = to_utc_iso(now - timedelta(hours=24))
    since_7d_iso = to_utc_iso(now - timedelta(days=7))

    signals_24h = count_signals_since(conn, since_iso=since_24h_iso)
    signals_7d = count_signals_since(conn, since_iso=since_7d_iso)
    sell_signals_7d = count_sell_signals_since(conn, since_iso=since_7d_iso)

    # Counts use len() over the existing readers because they
    # already return all-active rows (and active rows are always
    # tiny: at most one watch per symbol, a handful of open buys).
    # Re-implementing with dedicated COUNT(*) queries would
    # contradict the "reuse readers verbatim" rule of the design.
    watchlist_active = len(list_watching(conn))
    open_buys = len(load_open_buys(conn))

    regime = _build_regime(conn)
    analytics = _build_analytics(conn, now=now)
    recent_activity = _build_recent_activity(conn)

    return OverviewData(
        signals_24h=signals_24h,
        signals_7d=signals_7d,
        watchlist_active=watchlist_active,
        open_buys=open_buys,
        sell_signals_7d=sell_signals_7d,
        regime=regime,
        analytics=analytics,
        recent_activity=recent_activity,
    )


# ---------- internals ----------

def _build_regime(conn: sqlite3.Connection) -> OverviewRegime | None:
    snap = load_latest_regime(conn)
    if snap is None:
        return None
    return OverviewRegime(
        label=snap.label,  # type: ignore[arg-type]
        determined_at=snap.determined_at,
        atr_percentile=snap.atr_percentile,
    )


def _build_analytics(
    conn: sqlite3.Connection,
    *,
    now: datetime,
) -> OverviewAnalytics:
    rows = load_evaluation_rows(conn, scope="90d", now=now)
    report = compute_expectancy(rows, min_signals=5)
    overall = report.overall
    return OverviewAnalytics(
        scope="90d",
        total_signals=report.total_signals,
        win_rate=overall.win_rate,
        expectancy=overall.expectancy,
        profit_factor=overall.profit_factor,
    )


def _build_recent_activity(
    conn: sqlite3.Connection,
) -> list[ActivityItem]:
    """Merge the most recent signals + sell signals, newest first.

    Both source tables already provide ``ORDER BY detected_at DESC``
    via their respective readers; we slice to ``_ACTIVITY_LIMIT``
    after merging so the response size is bounded regardless of
    table volume.
    """
    items: list[ActivityItem] = []

    for row in list_recent_signals(conn, limit=_ACTIVITY_LIMIT):
        score = row["score"]
        sev = row["severity"] or "below_threshold"
        items.append(ActivityItem(
            kind="signal",
            id=int(row["id"]),
            at=str(row["detected_at"]),
            symbol=str(row["symbol"]),
            headline=f"{sev} score={score}",
        ))

    for row in list_recent_sell_signals(conn, limit=_ACTIVITY_LIMIT):
        rule = row["rule_triggered"]
        pnl = row["pnl_pct"]
        pnl_part = f" {_signed_pct(pnl)}" if pnl is not None else ""
        items.append(ActivityItem(
            kind="sell",
            id=int(row["id"]),
            at=str(row["detected_at"]),
            symbol=str(row["symbol"]),
            headline=f"{rule}{pnl_part}",
        ))

    items.sort(key=lambda i: i.at, reverse=True)
    return items[:_ACTIVITY_LIMIT]


def _signed_pct(value: float) -> str:
    """Format a signed percent (matches the analytics reporter)."""
    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"
