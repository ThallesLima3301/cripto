"""FastAPI app for the dashboard adapter.

Step 1 surface:

  * GET ``/api/health``    — DB reachability + freshness probe.
  * GET ``/api/overview``  — KPIs + recent-activity feed for the
                             dashboard home page.

Run locally with::

    uvicorn crypto_monitor.dashboard.api:app --reload --port 8787

The app intentionally binds to ``127.0.0.1`` by convention (uvicorn's
default). When this layer eventually moves off localhost, an auth
dependency lands here — for now the API is unauthenticated.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, TypeVar

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from crypto_monitor.dashboard.deps import get_db
from crypto_monitor.dashboard.schemas import (
    AnalyticsData,
    BuyItem,
    Envelope,
    HealthData,
    OpenBuyItem,
    OverviewData,
    PageMeta,
    RegimeItem,
    SellSignalItem,
    SignalDetail,
    SignalListItem,
    WatchlistItem,
    WeeklySummaryItem,
)
from crypto_monitor.dashboard.services import (
    build_analytics,
    build_buys_page,
    build_health,
    build_open_buys,
    build_overview,
    build_regime_history,
    build_regime_latest,
    build_sell_signals_page,
    build_signal_detail,
    build_signals_page,
    build_watchlist,
    build_weekly_summaries,
)


T = TypeVar("T")


logger = logging.getLogger(__name__)


app = FastAPI(
    title="crypto_monitor dashboard API",
    description=(
        "Read-only adapter over the crypto_monitor SQLite database. "
        "Designed to be consumed by a Next.js frontend."
    ),
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


# CORS: open during local dev so a Next.js dev server on
# http://localhost:3000 can call this API on http://localhost:8787
# without extra config. When the API leaves localhost, the
# allow_origins list narrows to the deployed frontend domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------- helpers ----------

def _guard_db(label: str, fn: Callable[[], T]) -> T:
    """Run a service function, converting locked-DB errors to 503.

    SQLite returns ``OperationalError: database is locked`` while a
    scheduled scan is mid-write; the dashboard surfaces that as a
    transient 503 so the frontend can retry instead of erroring hard.
    """
    try:
        return fn()
    except sqlite3.OperationalError as exc:
        logger.warning("%s hit a locked DB: %s", label, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database temporarily unavailable",
        ) from exc


# ---------- routes (Step 1 — health + overview) ----------

@app.get(
    "/api/health",
    response_model=Envelope[HealthData],
    tags=["meta"],
    summary="Liveness probe + DB freshness indicator.",
)
def health(conn: sqlite3.Connection = Depends(get_db)) -> Envelope[HealthData]:
    """Return ``status='ok'`` when the DB connection works."""
    data = _guard_db("health", lambda: build_health(conn))
    return Envelope[HealthData](data=data)


@app.get(
    "/api/overview",
    response_model=Envelope[OverviewData],
    tags=["overview"],
    summary="Dashboard home: KPIs, regime, analytics digest, activity feed.",
)
def overview(
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[OverviewData]:
    """Aggregate every widget on the dashboard home page in one call."""
    data = _guard_db("overview", lambda: build_overview(conn))
    return Envelope[OverviewData](
        data=data,
        meta={"analytics_scope": "90d"},
    )


# ---------- routes (Step 2 — list / detail surface) ----------

@app.get(
    "/api/signals",
    response_model=Envelope[list[SignalListItem]],
    tags=["signals"],
    summary="List signals with filters + pagination.",
)
def signals_list(
    symbol: str | None = Query(None),
    severity: str | None = Query(None),
    regime: str | None = Query(None, alias="regime"),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[SignalListItem]]:
    items, meta = _guard_db(
        "signals_list",
        lambda: build_signals_page(
            conn,
            symbol=symbol, severity=severity, regime=regime,
            since_iso=from_, until_iso=to,
            limit=limit, offset=offset,
        ),
    )
    return Envelope[list[SignalListItem]](data=items, meta=meta.model_dump())


@app.get(
    "/api/signals/{signal_id}",
    response_model=Envelope[SignalDetail],
    tags=["signals"],
    summary="Signal detail with optional evaluation block.",
    responses={404: {"description": "signal not found"}},
)
def signal_detail(
    signal_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[SignalDetail]:
    item = _guard_db(
        "signal_detail",
        lambda: build_signal_detail(conn, signal_id),
    )
    if item is None:
        raise HTTPException(status_code=404, detail=f"signal {signal_id} not found")
    return Envelope[SignalDetail](data=item)


@app.get(
    "/api/watchlist",
    response_model=Envelope[list[WatchlistItem]],
    tags=["watchlist"],
    summary="Active watchlist entries (status='watching').",
)
def watchlist(
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[WatchlistItem]]:
    items = _guard_db("watchlist", lambda: build_watchlist(conn))
    return Envelope[list[WatchlistItem]](data=items)


@app.get(
    "/api/open-buys",
    response_model=Envelope[list[OpenBuyItem]],
    tags=["sell"],
    summary="Open buys enriched for the sell-monitor page.",
)
def open_buys(
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[OpenBuyItem]]:
    items = _guard_db("open_buys", lambda: build_open_buys(conn))
    return Envelope[list[OpenBuyItem]](data=items)


@app.get(
    "/api/buys",
    response_model=Envelope[list[BuyItem]],
    tags=["buys"],
    summary="All buys with optional status / symbol / pagination filters.",
)
def buys_list(
    status_: str = Query("all", alias="status",
                         pattern="^(open|sold|all)$"),
    symbol: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[BuyItem]]:
    items, meta = _guard_db(
        "buys_list",
        lambda: build_buys_page(
            conn, status=status_, symbol=symbol,
            limit=limit, offset=offset,
        ),
    )
    return Envelope[list[BuyItem]](data=items, meta=meta.model_dump())


@app.get(
    "/api/sell-signals",
    response_model=Envelope[list[SellSignalItem]],
    tags=["sell"],
    summary="List sell-side signals with filters + pagination.",
)
def sell_signals_list(
    symbol: str | None = Query(None),
    rule: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[SellSignalItem]]:
    items, meta = _guard_db(
        "sell_signals_list",
        lambda: build_sell_signals_page(
            conn,
            symbol=symbol, rule=rule,
            since_iso=from_, until_iso=to,
            limit=limit, offset=offset,
        ),
    )
    return Envelope[list[SellSignalItem]](data=items, meta=meta.model_dump())


@app.get(
    "/api/analytics",
    response_model=Envelope[AnalyticsData],
    tags=["analytics"],
    summary="Expectancy report (overall + sliced).",
)
def analytics(
    scope: str = Query("all", pattern="^(all|90d|30d)$"),
    min_signals: int = Query(5, ge=1, le=100),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[AnalyticsData]:
    data = _guard_db(
        "analytics",
        lambda: build_analytics(conn, scope=scope, min_signals=min_signals),
    )
    return Envelope[AnalyticsData](
        data=data,
        meta={"scope": scope, "min_signals": min_signals},
    )


@app.get(
    "/api/weekly-summaries",
    response_model=Envelope[list[WeeklySummaryItem]],
    tags=["reports"],
    summary="Recent weekly summaries (newest first).",
)
def weekly_summaries(
    limit: int = Query(20, ge=1, le=200),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[WeeklySummaryItem]]:
    items = _guard_db(
        "weekly_summaries",
        lambda: build_weekly_summaries(conn, limit=limit),
    )
    return Envelope[list[WeeklySummaryItem]](
        data=items, meta={"limit": limit},
    )


@app.get(
    "/api/regime/latest",
    response_model=Envelope[RegimeItem | None],
    tags=["regime"],
    summary="Most recent regime snapshot, or null if absent.",
)
def regime_latest(
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[RegimeItem | None]:
    data = _guard_db("regime_latest", lambda: build_regime_latest(conn))
    return Envelope[RegimeItem | None](data=data)


@app.get(
    "/api/regime/history",
    response_model=Envelope[list[RegimeItem]],
    tags=["regime"],
    summary="Recent regime snapshots for the timeline chart.",
)
def regime_history(
    limit: int = Query(50, ge=1, le=500),
    conn: sqlite3.Connection = Depends(get_db),
) -> Envelope[list[RegimeItem]]:
    items = _guard_db(
        "regime_history",
        lambda: build_regime_history(conn, limit=limit),
    )
    return Envelope[list[RegimeItem]](
        data=items, meta={"limit": limit},
    )
