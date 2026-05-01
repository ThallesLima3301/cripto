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
from crypto_monitor.buys import count_buys, list_buys
from crypto_monitor.dashboard.schemas import (
    ActivityItem,
    AnalyticsBucket,
    AnalyticsData,
    BuyItem,
    HealthData,
    OpenBuyItem,
    OverviewAnalytics,
    OverviewData,
    OverviewRegime,
    PageMeta,
    RegimeItem,
    SellSignalItem,
    SignalDetail,
    SignalEvaluation,
    SignalListItem,
    WatchlistItem,
    WeeklySummaryItem,
)
from crypto_monitor.database.schema import get_schema_version
from crypto_monitor.regime.store import (
    list_regime_history,
    load_latest_regime,
)
from crypto_monitor.reports.weekly import list_weekly_summaries
from crypto_monitor.sell.store import (
    count_sell_signals,
    count_sell_signals_since,
    get_high_watermark,
    list_recent_sell_signals,
    list_sell_signals,
    load_open_buys,
)
from crypto_monitor.signals.persistence import (
    count_signals,
    count_signals_since,
    get_signal_detail,
    latest_candle_close_time,
    latest_close_for_symbol,
    list_recent_signals,
    list_signals,
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


# =====================================================================
# Step 2 — list / detail services
# =====================================================================

def _page_meta(*, total: int, limit: int, offset: int) -> PageMeta:
    """Compute the canonical pagination meta block once.

    ``next_offset`` is ``None`` when the next page would be empty, so
    the frontend stops paging without re-doing arithmetic.
    """
    next_offset = offset + limit if offset + limit < total else None
    return PageMeta(
        total=total, limit=limit, offset=offset, next_offset=next_offset,
    )


# ---------- /api/signals ----------

def build_signals_page(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    severity: str | None = None,
    regime: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SignalListItem], PageMeta]:
    """Return one page of signals + pagination metadata."""
    rows = list_signals(
        conn,
        symbol=symbol, severity=severity, regime=regime,
        since_iso=since_iso, until_iso=until_iso,
        limit=limit, offset=offset,
    )
    total = count_signals(
        conn,
        symbol=symbol, severity=severity, regime=regime,
        since_iso=since_iso, until_iso=until_iso,
    )
    items = [SignalListItem(**dict(r)) for r in rows]
    return items, _page_meta(total=total, limit=limit, offset=offset)


def build_signal_detail(
    conn: sqlite3.Connection,
    signal_id: int,
) -> SignalDetail | None:
    """Return one signal's detail view (with optional evaluation)."""
    import json
    row = get_signal_detail(conn, signal_id)
    if row is None:
        return None
    raw_breakdown = row["score_breakdown"]
    try:
        breakdown = json.loads(raw_breakdown) if raw_breakdown else {}
    except (TypeError, ValueError):
        breakdown = {}

    eval_block = _maybe_signal_evaluation(row)
    return SignalDetail(
        id=int(row["id"]),
        symbol=str(row["symbol"]),
        detected_at=str(row["detected_at"]),
        candle_hour=str(row["candle_hour"]),
        price_at_signal=float(row["price_at_signal"]),
        score=int(row["score"]),
        severity=row["severity"],
        trigger_reason=row["trigger_reason"],
        dominant_trigger_timeframe=row["dominant_trigger_timeframe"],
        drop_trigger_pct=row["drop_trigger_pct"],
        drop_24h_pct=row["drop_24h_pct"],
        drop_7d_pct=row["drop_7d_pct"],
        drop_30d_pct=row["drop_30d_pct"],
        drop_180d_pct=row["drop_180d_pct"],
        distance_from_30d_high_pct=row["distance_from_30d_high_pct"],
        distance_from_180d_high_pct=row["distance_from_180d_high_pct"],
        rsi_1h=row["rsi_1h"],
        rsi_4h=row["rsi_4h"],
        rel_volume=row["rel_volume"],
        dist_support_pct=row["dist_support_pct"],
        support_level_price=row["support_level_price"],
        reversal_signal=bool(row["reversal_signal"]),
        trend_context_4h=row["trend_context_4h"],
        trend_context_1d=row["trend_context_1d"],
        regime_at_signal=row["regime_at_signal"],
        watchlist_id=row["watchlist_id"],
        score_breakdown=breakdown,
        evaluation=eval_block,
    )


def _maybe_signal_evaluation(row) -> SignalEvaluation | None:
    """Build a :class:`SignalEvaluation` from the join columns or None."""
    if row["eval_evaluated_at"] is None:
        return None
    return SignalEvaluation(
        evaluated_at=row["eval_evaluated_at"],
        return_24h_pct=row["eval_return_24h_pct"],
        return_7d_pct=row["eval_return_7d_pct"],
        return_30d_pct=row["eval_return_30d_pct"],
        max_gain_7d_pct=row["eval_max_gain_7d_pct"],
        max_loss_7d_pct=row["eval_max_loss_7d_pct"],
        time_to_mfe_hours=row["eval_time_to_mfe_hours"],
        time_to_mae_hours=row["eval_time_to_mae_hours"],
        verdict=row["eval_verdict"],
    )


# ---------- /api/watchlist ----------

def build_watchlist(conn: sqlite3.Connection) -> list[WatchlistItem]:
    """Return active watchlist entries, oldest first.

    The store helper already filters to ``status='watching'`` and
    sorts ``first_seen_at ASC`` so the API is just a serializer.
    """
    return [
        WatchlistItem(
            id=e.id,
            symbol=e.symbol,
            status=e.status,  # type: ignore[arg-type]
            first_seen_at=e.first_seen_at,
            last_seen_at=e.last_seen_at,
            last_score=e.last_score,
            expires_at=e.expires_at,
            promoted_signal_id=e.promoted_signal_id,
            resolved_at=e.resolved_at,
            resolution_reason=e.resolution_reason,
        )
        for e in list_watching(conn)
    ]


# ---------- /api/open-buys ----------

def build_open_buys(conn: sqlite3.Connection) -> list[OpenBuyItem]:
    """Return open buys enriched with watermark + current-price view.

    Each row independently looks up its symbol's latest 1h close. With
    a small open-position count (typically <10) this stays cheap.
    """
    out: list[OpenBuyItem] = []
    for buy in load_open_buys(conn):
        watermark = get_high_watermark(
            conn, symbol=buy.symbol, buy_id=buy.id,
        )
        price_pair = latest_close_for_symbol(
            conn, buy.symbol, interval="1h",
        )
        current_price = price_pair[0] if price_pair is not None else None
        latest_close_at = price_pair[1] if price_pair is not None else None
        pnl_pct = _pct_change(buy.price, current_price)
        drawdown_pct = (
            _pct_change(watermark, current_price)
            if watermark is not None and current_price is not None
            else None
        )
        out.append(OpenBuyItem(
            id=buy.id,
            symbol=buy.symbol,
            bought_at=buy.bought_at,
            price=buy.price,
            quantity=buy.quantity,
            amount_invested=buy.amount_invested,
            quote_currency=buy.quote_currency,
            note=buy.note,
            high_watermark=watermark,
            current_price=current_price,
            latest_close_at=latest_close_at,
            pnl_pct=pnl_pct,
            drawdown_from_high_pct=drawdown_pct,
        ))
    return out


def _pct_change(base: float | None, current: float | None) -> float | None:
    if base is None or current is None or base == 0:
        return None
    return (current - base) / base * 100.0


# ---------- /api/buys ----------

def build_buys_page(
    conn: sqlite3.Connection,
    *,
    status: str = "all",
    symbol: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BuyItem], PageMeta]:
    records = list_buys(
        conn, symbol=symbol, status=status,
        limit=limit, offset=offset,
    )
    total = count_buys(conn, symbol=symbol, status=status)
    items = [
        BuyItem(
            id=r.id,
            symbol=r.symbol,
            bought_at=r.bought_at,
            price=r.price,
            amount_invested=r.amount_invested,
            quote_currency=r.quote_currency,
            quantity=r.quantity,
            signal_id=r.signal_id,
            note=r.note,
            created_at=r.created_at,
            sold_at=r.sold_at,
            sold_price=r.sold_price,
            sold_note=r.sold_note,
        )
        for r in records
    ]
    return items, _page_meta(total=total, limit=limit, offset=offset)


# ---------- /api/sell-signals ----------

def build_sell_signals_page(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    rule: str | None = None,
    since_iso: str | None = None,
    until_iso: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SellSignalItem], PageMeta]:
    rows = list_sell_signals(
        conn,
        symbol=symbol, rule=rule,
        since_iso=since_iso, until_iso=until_iso,
        limit=limit, offset=offset,
    )
    total = count_sell_signals(
        conn,
        symbol=symbol, rule=rule,
        since_iso=since_iso, until_iso=until_iso,
    )
    items = [SellSignalItem(**dict(r)) for r in rows]
    return items, _page_meta(total=total, limit=limit, offset=offset)


# ---------- /api/analytics ----------

def build_analytics(
    conn: sqlite3.Connection,
    *,
    scope: str = "all",
    min_signals: int = 5,
    now: datetime | None = None,
) -> AnalyticsData:
    """Reuse the analytics aggregator verbatim, then serialize."""
    rows = load_evaluation_rows(
        conn, scope=scope,  # type: ignore[arg-type]
        now=now or now_utc(),
    )
    report = compute_expectancy(rows, min_signals=max(1, min_signals))

    def _bucket(b) -> AnalyticsBucket:
        return AnalyticsBucket(
            count=b.count,
            win_rate=b.win_rate,
            avg_win_pct=b.avg_win_pct,
            avg_loss_pct=b.avg_loss_pct,
            expectancy=b.expectancy,
            profit_factor=b.profit_factor,
            avg_mfe_pct=b.avg_mfe_pct,
            avg_mae_pct=b.avg_mae_pct,
            avg_time_to_mfe_hours=b.avg_time_to_mfe_hours,
            avg_time_to_mae_hours=b.avg_time_to_mae_hours,
        )

    return AnalyticsData(
        total_signals=report.total_signals,
        overall=_bucket(report.overall),
        by_severity={k: _bucket(v) for k, v in report.by_severity.items()},
        by_regime={k: _bucket(v) for k, v in report.by_regime.items()},
        by_score_bucket={
            k: _bucket(v) for k, v in report.by_score_bucket.items()
        },
        by_dominant_trigger={
            k: _bucket(v) for k, v in report.by_dominant_trigger.items()
        },
    )


# ---------- /api/weekly-summaries ----------

def build_weekly_summaries(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
) -> list[WeeklySummaryItem]:
    return [
        WeeklySummaryItem(
            id=int(r["id"]),
            week_start=str(r["week_start"]),
            week_end=str(r["week_end"]),
            generated_at=str(r["generated_at"]),
            body=str(r["body"]),
            signal_count=int(r["signal_count"]),
            buy_count=int(r["buy_count"]),
            top_drop_symbol=r["top_drop_symbol"],
            top_drop_pct=r["top_drop_pct"],
            sent=int(r["sent"]),
        )
        for r in list_weekly_summaries(conn, limit=limit)
    ]


# ---------- /api/regime/{latest,history} ----------

def build_regime_latest(conn: sqlite3.Connection) -> RegimeItem | None:
    snap = load_latest_regime(conn)
    if snap is None:
        return None
    return _regime_item_from_snapshot(snap)


def build_regime_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> list[RegimeItem]:
    return [_regime_item_from_snapshot(s)
            for s in list_regime_history(conn, limit=limit)]


def _regime_item_from_snapshot(snap) -> RegimeItem:
    return RegimeItem(
        label=snap.label,  # type: ignore[arg-type]
        btc_ema_short=snap.btc_ema_short,
        btc_ema_long=snap.btc_ema_long,
        btc_atr_14d=snap.btc_atr_14d,
        atr_percentile=snap.atr_percentile,
        determined_at=snap.determined_at,
    )
