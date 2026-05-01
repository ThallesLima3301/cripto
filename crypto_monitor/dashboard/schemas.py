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


# =====================================================================
# Step 2 — list / detail endpoints
# =====================================================================

# ---------- shared pagination meta ----------

class PageMeta(BaseModel):
    """Pagination metadata returned in the envelope's ``meta`` block.

    ``next_offset`` is ``None`` when ``offset + limit >= total`` so the
    frontend can stop paging without computing the math itself.
    """
    model_config = ConfigDict(frozen=True)

    total: int
    limit: int
    offset: int
    next_offset: int | None


# ---------- /api/signals + /api/signals/{id} ----------

class SignalListItem(BaseModel):
    """One row on the signals list page."""
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    detected_at: str
    candle_hour: str
    price_at_signal: float
    score: int
    severity: str | None
    trigger_reason: str | None
    dominant_trigger_timeframe: str | None
    drop_trigger_pct: float | None
    rsi_1h: float | None
    rsi_4h: float | None
    rel_volume: float | None
    regime_at_signal: str | None
    watchlist_id: int | None


class SignalEvaluation(BaseModel):
    """Evaluation block embedded inside ``/api/signals/{id}``.

    Every field is optional because the join is left-outer: a signal
    that hasn't matured yet has no evaluation row, and an evaluation
    row may legitimately have NULLs (insufficient post-event candles).
    """
    model_config = ConfigDict(frozen=True)

    evaluated_at: str | None = None
    return_24h_pct: float | None = None
    return_7d_pct: float | None = None
    return_30d_pct: float | None = None
    max_gain_7d_pct: float | None = None
    max_loss_7d_pct: float | None = None
    time_to_mfe_hours: float | None = None
    time_to_mae_hours: float | None = None
    verdict: str | None = None


class SignalDetail(BaseModel):
    """Rich payload for ``/api/signals/{id}``.

    Extends ``SignalListItem`` with the full set of factor numbers and
    the optional evaluation block. The frontend signal-detail page
    renders ``score_breakdown`` as a tree without a second fetch.
    """
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    detected_at: str
    candle_hour: str
    price_at_signal: float
    score: int
    severity: str | None
    trigger_reason: str | None
    dominant_trigger_timeframe: str | None
    drop_trigger_pct: float | None
    drop_24h_pct: float | None
    drop_7d_pct: float | None
    drop_30d_pct: float | None
    drop_180d_pct: float | None
    distance_from_30d_high_pct: float | None
    distance_from_180d_high_pct: float | None
    rsi_1h: float | None
    rsi_4h: float | None
    rel_volume: float | None
    dist_support_pct: float | None
    support_level_price: float | None
    reversal_signal: bool
    trend_context_4h: str | None
    trend_context_1d: str | None
    regime_at_signal: str | None
    watchlist_id: int | None
    score_breakdown: dict[str, Any]
    evaluation: SignalEvaluation | None


# ---------- /api/watchlist ----------

class WatchlistItem(BaseModel):
    """One row from the ``watchlist`` table.

    Mirrors :class:`crypto_monitor.watchlist.WatchlistEntry` field for
    field, but the API ships a stable schema so internal renames don't
    break the frontend.
    """
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    status: Literal["watching", "promoted", "expired"]
    first_seen_at: str
    last_seen_at: str
    last_score: int
    expires_at: str
    promoted_signal_id: int | None
    resolved_at: str | None
    resolution_reason: str | None


# ---------- /api/open-buys ----------

class OpenBuyItem(BaseModel):
    """One open buy enriched for the sell-monitor page.

    ``current_price`` and ``latest_close_at`` are best-effort: they
    reflect the latest 1h candle, which may be up to 60 minutes old.
    The frontend renders ``latest_close_at`` so the staleness is
    explicit. ``high_watermark`` is the post-entry peak the sell
    engine tracks for the trailing-stop rule.

    All derived percent fields stay ``None`` when ``current_price`` is
    unavailable (no candle for the symbol yet).
    """
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    bought_at: str
    price: float
    quantity: float
    amount_invested: float
    quote_currency: str
    note: str | None
    high_watermark: float | None
    current_price: float | None
    latest_close_at: str | None
    pnl_pct: float | None
    drawdown_from_high_pct: float | None


# ---------- /api/buys ----------

class BuyItem(BaseModel):
    """One row from the ``buys`` table including sold-out columns."""
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    bought_at: str
    price: float
    amount_invested: float
    quote_currency: str
    quantity: float
    signal_id: int | None
    note: str | None
    created_at: str
    sold_at: str | None
    sold_price: float | None
    sold_note: str | None


# ---------- /api/sell-signals ----------

class SellSignalItem(BaseModel):
    """One row from the ``sell_signals`` log."""
    model_config = ConfigDict(frozen=True)

    id: int
    symbol: str
    buy_id: int
    rule_triggered: str
    severity: str
    detected_at: str
    price_at_signal: float
    pnl_pct: float | None
    regime_at_signal: str | None
    reason: str | None
    alerted: int


# ---------- /api/analytics ----------

class AnalyticsBucket(BaseModel):
    """Mirrors :class:`crypto_monitor.analytics.ExpectancyBucket`."""
    model_config = ConfigDict(frozen=True)

    count: int
    win_rate: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    expectancy: float | None
    profit_factor: float | None
    avg_mfe_pct: float | None
    avg_mae_pct: float | None
    avg_time_to_mfe_hours: float | None
    avg_time_to_mae_hours: float | None


class AnalyticsData(BaseModel):
    """Top-level payload for ``/api/analytics``."""
    model_config = ConfigDict(frozen=True)

    total_signals: int
    overall: AnalyticsBucket
    by_severity: dict[str, AnalyticsBucket]
    by_regime: dict[str, AnalyticsBucket]
    by_score_bucket: dict[str, AnalyticsBucket]
    by_dominant_trigger: dict[str, AnalyticsBucket]


# ---------- /api/weekly-summaries ----------

class WeeklySummaryItem(BaseModel):
    """One row from the ``weekly_summaries`` table."""
    model_config = ConfigDict(frozen=True)

    id: int
    week_start: str
    week_end: str
    generated_at: str
    body: str
    signal_count: int
    buy_count: int
    top_drop_symbol: str | None
    top_drop_pct: float | None
    sent: int


# ---------- /api/regime/{latest,history} ----------

class RegimeItem(BaseModel):
    """One regime snapshot. Used for both /latest and /history."""
    model_config = ConfigDict(frozen=True)

    label: Literal["risk_on", "neutral", "risk_off"]
    btc_ema_short: float
    btc_ema_long: float
    btc_atr_14d: float
    atr_percentile: float
    determined_at: str
