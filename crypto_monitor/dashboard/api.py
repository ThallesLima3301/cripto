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

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from crypto_monitor.dashboard.deps import get_db
from crypto_monitor.dashboard.schemas import (
    Envelope,
    HealthData,
    OverviewData,
)
from crypto_monitor.dashboard.services import build_health, build_overview


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


# ---------- routes ----------

@app.get(
    "/api/health",
    response_model=Envelope[HealthData],
    tags=["meta"],
    summary="Liveness probe + DB freshness indicator.",
)
def health(conn: sqlite3.Connection = Depends(get_db)) -> Envelope[HealthData]:
    """Return ``status='ok'`` when the DB connection works.

    Catches ``sqlite3.OperationalError`` (typically "database is
    locked" during a bot scan) and returns ``503`` so the frontend
    can render a "retrying…" state instead of a generic 500.
    """
    try:
        data = build_health(conn)
    except sqlite3.OperationalError as exc:
        logger.warning("health check hit a locked DB: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database temporarily unavailable",
        ) from exc
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
    """Aggregate every widget on the dashboard home page in one call.

    See :func:`crypto_monitor.dashboard.services.build_overview` for
    the composition. Each widget tolerates "no data yet" so a fresh
    install returns a valid response with zero counts and ``None``
    analytics — matching the rest of the project's "graceful empty
    state" convention.
    """
    try:
        data = build_overview(conn)
    except sqlite3.OperationalError as exc:
        logger.warning("overview hit a locked DB: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database temporarily unavailable",
        ) from exc
    return Envelope[OverviewData](
        data=data,
        meta={"analytics_scope": "90d"},
    )
