"""Pydantic response schemas — the stable wire contract.

Every dataclass that lives inside the bot (``BuyRecord``,
``WatchlistEntry``, ``RegimeSnapshot``, ``ExpectancyBucket``, …) is
free to evolve. The schemas in this module are what the frontend
sees, so they evolve **deliberately**: a new field here is a UX
decision, never a side-effect of a refactor inside the core.

Conventions:

  * Every endpoint returns ``{"data": ..., "meta": {...}}``. The
    ``meta`` shape is endpoint-specific.
  * Timestamps stay as the same UTC ISO strings the database stores;
    the API never re-parses them.
  * Optional values that mean "not enough data" are typed as
    ``Optional[...]`` and surface as JSON ``null`` — a 0 would be
    indistinguishable from a real zero metric.

Step 1 only models the schemas needed by ``/api/health`` and
``/api/overview``. Later steps add per-endpoint response models in
this same module.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


T = TypeVar("T")


# ---------- generic envelope ----------

class Envelope(BaseModel, Generic[T]):
    """Standard ``{"data": ..., "meta": ...}`` wrapper.

    Pydantic v2 supports generic models natively. Using one envelope
    everywhere means the frontend's fetch wrapper has exactly one
    response shape to unwrap.
    """
    model_config = ConfigDict(frozen=True)

    data: T
    meta: dict[str, Any] = Field(default_factory=dict)


# ---------- /api/health ----------

class HealthData(BaseModel):
    """Health probe payload.

    ``status`` is ``"ok"`` whenever the request reached the database
    successfully. ``"degraded"`` is reserved for cases where the
    database round-trip succeeded but a downstream check (e.g. very
    stale candle data) would warn an operator.
    """
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    schema_version: int | None
    latest_candle_close_at: str | None = Field(
        None,
        description=(
            "close_time of the most recent 1h candle in the local DB. "
            "Useful as a freshness indicator: 'is the bot still running?'."
        ),
    )


# ---------- /api/overview ----------

class OverviewRegime(BaseModel):
    """Compact regime view rendered on the overview KPI strip."""
    model_config = ConfigDict(frozen=True)

    label: Literal["risk_on", "neutral", "risk_off"]
    determined_at: str
    atr_percentile: float


class OverviewAnalytics(BaseModel):
    """Headline metrics from the 90-day analytics scope.

    Every metric is optional because the analytics aggregator returns
    ``None`` when the input is empty / below threshold. The frontend
    renders ``null`` as "—" rather than "0%".
    """
    model_config = ConfigDict(frozen=True)

    scope: Literal["all", "90d", "30d"] = "90d"
    total_signals: int
    win_rate: float | None
    expectancy: float | None
    profit_factor: float | None


class ActivityItem(BaseModel):
    """One entry in the recent-activity feed.

    ``kind`` distinguishes the source so the UI can pick an icon
    without parsing the headline. ``id`` lets the UI link to a future
    detail route.
    """
    model_config = ConfigDict(frozen=True)

    kind: Literal["signal", "sell"]
    id: int
    at: str
    symbol: str
    headline: str


class OverviewData(BaseModel):
    """Top-level payload for ``/api/overview``."""
    model_config = ConfigDict(frozen=True)

    signals_24h: int
    signals_7d: int
    watchlist_active: int
    open_buys: int
    sell_signals_7d: int
    regime: OverviewRegime | None
    analytics: OverviewAnalytics
    recent_activity: list[ActivityItem]
