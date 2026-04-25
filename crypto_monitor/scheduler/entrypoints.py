"""Scheduler entrypoint orchestrators.

Each function here is the top of a pipeline the Windows Task
Scheduler (or any cron invoker) fires on a schedule:

  * `run_scan`         — every 5 minutes: flush queued notifications,
                         ingest new candles, score + persist any new
                         signals, then dispatch pending alerts.
  * `run_weekly`       — once per week: generate a weekly summary,
                         persist it, push it to ntfy.
  * `run_maintenance`  — nightly (or hourly): evaluate pending signals
                         and buys that just matured, prune old candle
                         rows, optionally VACUUM.

They are intentionally thin. The heavy lifting lives in:

  * `crypto_monitor.ingestion.market.ingest_all_symbols`
  * `crypto_monitor.signals.engine.score_signal` +
    `crypto_monitor.signals.persistence.insert_signal`
  * `crypto_monitor.notifications.service.process_pending_signals` +
    `flush_queue`
  * `crypto_monitor.evaluation.*.evaluate_pending_*`
  * `crypto_monitor.reports.weekly.generate_and_send_weekly_summary`
  * `crypto_monitor.database.retention.prune_old_candles` / `vacuum`

No entrypoint is allowed to re-implement any of those. If a piece of
behavior belongs somewhere else and we notice it here, it should move
out — this module is purely the "glue" layer.

Dependency injection
--------------------
Every entrypoint accepts optional injected resources (`conn`,
`client`, `sender`, `now`) so tests can drive the whole pipeline
against an in-memory DB with stub HTTP. In production the defaults
take over: open a real DB connection off `settings.general.db_path`,
construct a real BinanceClient, and use the real `send_ntfy`.

Connection ownership: if the caller injects a `conn`, we do NOT close
it — the caller is responsible. If the entrypoint opens its own
connection, it closes it on the way out, even on error.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from crypto_monitor.binance.client import BinanceClient
from crypto_monitor.config.settings import Settings, load_settings
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.retention import (
    PruneReport,
    prune_old_candles,
    vacuum,
)
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db, seed_default_symbols
from crypto_monitor.evaluation import (
    BuyEvalReport,
    SignalEvalReport,
    evaluate_pending_buys,
    evaluate_pending_signals,
)
from crypto_monitor.ingestion.market import IngestReport, ingest_all_symbols
from crypto_monitor.notifications.ntfy import SendResult, send_ntfy
from crypto_monitor.notifications.service import (
    FlushReport,
    ProcessReport,
    flush_queue,
    process_pending_signals,
)
from crypto_monitor.reports.weekly import (
    WeeklyRunResult,
    generate_and_send_weekly_summary,
)
from crypto_monitor.regime import RegimeSnapshot, classify_regime, save_regime_snapshot
from crypto_monitor.sell.runtime import ProcessSellReport, process_open_positions
from crypto_monitor.signals.engine import score_signal
from crypto_monitor.signals.persistence import (
    REASON_INSERTED,
    InsertResult,
    insert_signal,
    load_candles,
)
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso
from crypto_monitor.watchlist import (
    EXPIRE,
    PROMOTE,
    WATCH,
    decide_watch_action,
    expire_below_floor,
    expire_stale,
    get_watching,
    promote,
    upsert_watching,
)

_BTC_REGIME_SYMBOL = "BTCUSDT"


logger = logging.getLogger(__name__)


NtfySender = Callable[..., SendResult]
BinanceClientFactory = Callable[[], BinanceClient]


# ---------- report dataclasses ----------

@dataclass
class WatchlistReport:
    """Summary of one scan-cycle's watchlist activity."""
    watched: int = 0      # WATCH actions taken (insert + refresh)
    promoted: int = 0     # PROMOTE actions that successfully inserted
    expired_below_floor: int = 0
    expired_stale: int = 0
    ignored: int = 0


@dataclass
class ScanReport:
    """Summary of a `run_scan` run, collected for log output."""
    symbols_seeded: int = 0
    flush_report: FlushReport | None = None
    ingest_report: IngestReport | None = None
    scored_symbols: int = 0
    inserted_signals: int = 0
    signal_insert_reasons: dict[str, int] = field(default_factory=dict)
    process_report: ProcessReport | None = None
    regime_snapshot: RegimeSnapshot | None = None
    sell_report: ProcessSellReport | None = None
    watchlist_report: WatchlistReport | None = None
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        ingest_total = self.ingest_report.total_new if self.ingest_report else 0
        processed = (
            self.process_report.sent if self.process_report else 0
        )
        queued = self.process_report.queued if self.process_report else 0
        cd = (
            self.process_report.skipped_cooldown if self.process_report else 0
        )
        failed = (
            self.process_report.send_failed if self.process_report else 0
        )
        sell = self.sell_report
        sell_part = (
            f" sell_emitted={sell.signals_emitted} sell_sent={sell.signals_sent} "
            f"sell_cooldown={sell.cooldown_suppressed}"
            if sell is not None
            else ""
        )
        wl = self.watchlist_report
        wl_part = (
            f" wl_watched={wl.watched} wl_promoted={wl.promoted} "
            f"wl_expired={wl.expired_below_floor + wl.expired_stale}"
            if wl is not None
            else ""
        )
        return (
            f"scan ingest={ingest_total} scored={self.scored_symbols} "
            f"inserted={self.inserted_signals} "
            f"sent={processed} queued={queued} "
            f"cooldown={cd} failed={failed}{sell_part}{wl_part} "
            f"errors={len(self.errors)}"
        )


@dataclass
class MaintenanceReport:
    """Summary of a `run_maintenance` run."""
    signal_eval_report: SignalEvalReport | None = None
    buy_eval_report: BuyEvalReport | None = None
    prune_report: PruneReport | None = None
    vacuumed: bool = False
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        signals_eval = (
            self.signal_eval_report.evaluated if self.signal_eval_report else 0
        )
        signals_pending = (
            self.signal_eval_report.skipped_pending
            if self.signal_eval_report
            else 0
        )
        buys_eval = (
            self.buy_eval_report.evaluated if self.buy_eval_report else 0
        )
        buys_pending = (
            self.buy_eval_report.skipped_pending if self.buy_eval_report else 0
        )
        pruned = (
            self.prune_report.total_deleted if self.prune_report else 0
        )
        return (
            f"maintenance signals_evaluated={signals_eval} "
            f"signals_pending={signals_pending} "
            f"buys_evaluated={buys_eval} buys_pending={buys_pending} "
            f"pruned={pruned} vacuumed={self.vacuumed} "
            f"errors={len(self.errors)}"
        )


# ---------- scan ----------

def run_scan(
    project_root: Path | None = None,
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    client: BinanceClient | None = None,
    client_factory: BinanceClientFactory | None = None,
    now: datetime | None = None,
    sender: NtfySender | None = None,
) -> ScanReport:
    """Run one scan cycle end to end.

    Steps (in order):
      1. Load settings (or use the injected one).
      2. Open or accept a DB connection; run `init_db` idempotently.
      3. Seed default tracked symbols if `settings.symbols.auto_seed`.
      4. Flush any queued notifications (quiet hours may have ended).
      5. Ingest fresh candles from Binance.
      6. Score every active symbol; persist new signals with dedup.
      7. Process pending alerts (send / queue / cooldown).

    Returns a `ScanReport` suitable for a single log line. Raises
    only if no settings can be resolved.
    """
    settings = _resolve_settings(project_root, settings)
    if now is None:
        now = now_utc()

    report = ScanReport()
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(settings.general.db_path)
    assert conn is not None  # for type checkers

    try:
        init_db(conn)
        run_migrations(conn)

        # 3. optional seeding
        if settings.symbols.auto_seed:
            report.symbols_seeded = seed_default_symbols(
                conn, list(settings.symbols.tracked)
            )
            if report.symbols_seeded:
                logger.info(
                    "seeded %d tracked symbol(s)", report.symbols_seeded
                )

        # 3b. ensure BTC is seeded when regime is enabled
        if settings.regime.enabled:
            _ensure_btc_seeded(conn)

        # 4. flush queue
        try:
            report.flush_report = flush_queue(
                conn,
                alerts=settings.alerts,
                ntfy=settings.ntfy,
                timezone_name=settings.general.timezone,
                now=now,
                sender=sender,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("flush_queue failed")
            report.errors.append(f"flush_queue: {exc}")

        # 5. ingestion
        active_symbols = _list_active_symbols(conn)
        intervals = list(settings.intervals.tracked)
        try:
            if active_symbols and intervals:
                if client is None:
                    client = _build_default_client(settings, client_factory)
                report.ingest_report = ingest_all_symbols(
                    conn,
                    client,
                    active_symbols,
                    intervals,
                    bootstrap_limit=settings.intervals.bootstrap_limit,
                )
                if report.ingest_report.errors:
                    report.errors.extend(report.ingest_report.errors)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ingest_all_symbols failed")
            report.errors.append(f"ingest: {exc}")

        # 5b. regime classification (when enabled)
        regime: RegimeSnapshot | None = None
        if settings.regime.enabled:
            try:
                regime = _classify_regime(conn, settings)
                if regime is not None:
                    save_regime_snapshot(conn, regime)
                    report.regime_snapshot = regime
                    logger.info(
                        "regime: %s (ATR pctile=%.0f)",
                        regime.label,
                        regime.atr_percentile,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("regime classification failed")
                report.errors.append(f"regime: {exc}")

        # 6. scoring + dedup insert
        #    BTC is excluded from scoring when it was auto-seeded for
        #    regime classification only (not in the user's tracked list).
        scorable = [
            s for s in active_symbols
            if s != _BTC_REGIME_SYMBOL
            or _BTC_REGIME_SYMBOL in settings.symbols.tracked
        ]
        # Initialize the watchlist report up front when the feature is
        # on so the scoring loop has somewhere to record its decisions.
        if settings.watchlist.enabled:
            report.watchlist_report = WatchlistReport()
            try:
                report.watchlist_report.expired_stale = expire_stale(
                    conn, now=now,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("watchlist expire_stale failed")
                report.errors.append(f"watchlist: {exc}")

        try:
            _score_and_persist(
                conn,
                symbols=scorable,
                settings=settings,
                report=report,
                now=now,
                regime=regime,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("scoring pass failed")
            report.errors.append(f"scoring: {exc}")

        # 6b. sell-side pass (evaluate open buys, insert + notify sells)
        if settings.sell.enabled:
            try:
                regime_label_for_sell = (
                    regime.label if regime is not None else None
                )
                report.sell_report = process_open_positions(
                    conn,
                    settings=settings.sell,
                    ntfy=settings.ntfy,
                    regime_label=regime_label_for_sell,
                    now=now,
                    sender=sender,
                )
                if report.sell_report.errors:
                    report.errors.extend(report.sell_report.errors)
            except Exception as exc:  # noqa: BLE001
                logger.exception("process_open_positions failed")
                report.errors.append(f"sell: {exc}")

        # 7. alert processing
        try:
            report.process_report = process_pending_signals(
                conn,
                alerts=settings.alerts,
                ntfy=settings.ntfy,
                timezone_name=settings.general.timezone,
                now=now,
                sender=sender,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("process_pending_signals failed")
            report.errors.append(f"alerts: {exc}")

        logger.info(report.summary_line())
        return report
    finally:
        if owns_conn:
            conn.close()


# ---------- weekly ----------

def run_weekly(
    project_root: Path | None = None,
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
    sender: NtfySender | None = None,
) -> WeeklyRunResult:
    """Generate, persist, and send a weekly summary via ntfy.

    Thin wrapper: `generate_and_send_weekly_summary` already does the
    whole job. This function's only responsibility is settings /
    connection lifecycle + a single log line.
    """
    settings = _resolve_settings(project_root, settings)
    if now is None:
        now = now_utc()

    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(settings.general.db_path)
    assert conn is not None

    try:
        init_db(conn)
        run_migrations(conn)
        run = generate_and_send_weekly_summary(
            conn,
            ntfy=settings.ntfy,
            now=now,
            sender=sender,
        )
        logger.info(
            "weekly summary id=%d signals=%d buys=%d sent=%s",
            run.summary_id,
            run.summary.signal_count,
            run.summary.buy_count,
            run.send_result.sent,
        )
        return run
    finally:
        if owns_conn:
            conn.close()


# ---------- maintenance ----------

def run_maintenance(
    project_root: Path | None = None,
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> MaintenanceReport:
    """Evaluate matured signals/buys, prune old candles, optional VACUUM.

    Every step is wrapped so one failing phase does not prevent the
    rest from running. A corrupt evaluation should not stop the
    retention prune that keeps the DB from growing.
    """
    settings = _resolve_settings(project_root, settings)
    if now is None:
        now = now_utc()

    report = MaintenanceReport()
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(settings.general.db_path)
    assert conn is not None

    try:
        init_db(conn)
        run_migrations(conn)

        try:
            report.signal_eval_report = evaluate_pending_signals(
                conn,
                eval_settings=settings.evaluation,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluate_pending_signals failed")
            report.errors.append(f"signal_eval: {exc}")

        try:
            report.buy_eval_report = evaluate_pending_buys(
                conn,
                eval_settings=settings.evaluation,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluate_pending_buys failed")
            report.errors.append(f"buy_eval: {exc}")

        try:
            report.prune_report = prune_old_candles(conn, settings.retention)
        except Exception as exc:  # noqa: BLE001
            logger.exception("prune_old_candles failed")
            report.errors.append(f"prune: {exc}")

        if settings.retention.vacuum_on_maintenance:
            try:
                vacuum(conn)
                report.vacuumed = True
            except Exception as exc:  # noqa: BLE001
                logger.exception("VACUUM failed")
                report.errors.append(f"vacuum: {exc}")

        logger.info(report.summary_line())
        return report
    finally:
        if owns_conn:
            conn.close()


# ---------- internals ----------

def _resolve_settings(
    project_root: Path | None,
    settings: Settings | None,
) -> Settings:
    """Return the settings to use, loading from disk if needed.

    Tests always inject `settings` directly; production callers
    (Task Scheduler scripts via the CLI) pass `project_root`.
    """
    if settings is not None:
        return settings
    if project_root is None:
        raise ValueError(
            "Scheduler entrypoint requires either `project_root` or `settings`"
        )
    return load_settings(project_root)


def _build_default_client(
    settings: Settings,
    client_factory: BinanceClientFactory | None,
) -> BinanceClient:
    """Construct a real BinanceClient from settings, or delegate to a factory."""
    if client_factory is not None:
        return client_factory()
    return BinanceClient(
        base_url=settings.binance.base_url,
        timeout=settings.binance.request_timeout,
        retries=settings.binance.retry_count,
    )


def _ensure_btc_seeded(conn: sqlite3.Connection) -> None:
    """Ensure BTCUSDT is in the symbols table for regime candle ingestion.

    When regime is enabled, BTC daily candles must be available as a
    first-class persisted data dependency even if the user has not
    included BTCUSDT in ``[symbols].tracked``.  We seed it here
    (before ingestion) so the normal ingestion pipeline picks it up.
    Uses INSERT OR IGNORE, so it is a no-op when already present.
    """
    seed_default_symbols(conn, [_BTC_REGIME_SYMBOL])


def _classify_regime(
    conn: sqlite3.Connection,
    settings: Settings,
) -> RegimeSnapshot | None:
    """Run regime classification and return a snapshot (or None).

    Loads BTC 1d candles from the candles table (they are persisted
    there by the normal ingestion pipeline after ``_ensure_btc_seeded``
    runs before ingestion).

    Returns ``None`` if classification fails due to insufficient data.
    """
    btc_candles_1d = load_candles(conn, _BTC_REGIME_SYMBOL, "1d", limit=250)
    snapshot = classify_regime(
        btc_candles_1d,
        ema_short_period=settings.regime.ema_short_period,
        ema_long_period=settings.regime.ema_long_period,
        atr_period=settings.regime.atr_period,
        atr_lookback=settings.regime.atr_lookback,
        atr_high_percentile=settings.regime.atr_high_percentile,
    )
    if snapshot is None:
        logger.warning(
            "regime: insufficient BTC history (%d 1d candles, need %d)",
            len(btc_candles_1d),
            settings.regime.ema_long_period,
        )
    return snapshot


def _regime_min_score_adjust(
    regime: RegimeSnapshot | None,
    settings: Settings,
) -> int:
    """Return the emit-threshold adjustment for the current regime.

    ``risk_on``  -> ``settings.regime.threshold_adjust_risk_on``  (typically negative,
                    lowering the bar so more borderline signals emit).
    ``risk_off`` -> ``settings.regime.threshold_adjust_risk_off`` (typically positive,
                    raising the bar so only stronger signals emit).
    Anything else (``neutral``, ``None``, regime feature disabled) -> 0.
    """
    if regime is None or not settings.regime.enabled:
        return 0
    if regime.label == "risk_on":
        return settings.regime.threshold_adjust_risk_on
    if regime.label == "risk_off":
        return settings.regime.threshold_adjust_risk_off
    return 0


def _list_active_symbols(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT symbol FROM symbols
        WHERE active = 1
        ORDER BY symbol ASC
        """
    ).fetchall()
    return [r["symbol"] for r in rows]


def _score_and_persist(
    conn: sqlite3.Connection,
    *,
    symbols: list[str],
    settings: Settings,
    report: ScanReport,
    now: datetime,
    regime: RegimeSnapshot | None = None,
) -> None:
    """Score every active symbol and persist any new signals.

    This is the one and only place the scheduler stitches the
    (ingested candles) -> (signal engine) -> (dedup insert) pipeline
    together. Per-symbol errors are isolated so a single bad feed
    doesn't abort the whole scan.

    Block 23 added the watchlist branch: when ``settings.watchlist.enabled``
    is True and the candidate's severity is None (the regular emit path
    declined), the manager decides one of WATCH/PROMOTE/EXPIRE/IGNORE.
    PROMOTE synthesizes a severity from ``ScoringSeverity`` and re-runs
    through ``insert_signal`` so dedup, breakdown, and downstream alerts
    behave identically to a regular signal — but the row carries a
    ``watchlist_id`` linking back to the originating watch.

    We only need enough history to satisfy the longest lookback the
    engine might touch (180d on the 1d interval, 30d on the 1h
    interval for RSI tail, etc). Loading 250 per interval is the
    same budget `load_candles`'s default uses.
    """
    detected_at = to_utc_iso(now)
    regime_label = regime.label if regime is not None else None
    min_score_adjust = _regime_min_score_adjust(regime, settings)
    base_min_score = settings.scoring.thresholds.min_signal_score
    wl_enabled = settings.watchlist.enabled

    for symbol in symbols:
        try:
            candles_1h = load_candles(conn, symbol, "1h", limit=250)
            if not candles_1h:
                continue
            candles_4h = load_candles(conn, symbol, "4h", limit=250)
            candles_1d = load_candles(conn, symbol, "1d", limit=250)

            candidate = score_signal(
                symbol,
                candles_1h,
                candles_4h,
                candles_1d,
                settings.scoring,
                detected_at=detected_at,
                regime_at_signal=regime_label,
                min_score_adjust=min_score_adjust,
            )
            if candidate is None:
                continue
            report.scored_symbols += 1

            if candidate.severity is not None:
                # Regular emit path — unchanged behavior.
                result: InsertResult = insert_signal(conn, candidate)
                _record_insert_outcome(report, result)
                continue

            # Watchlist branch — runs only when the regular signal
            # didn't emit AND the feature is on. Disabled-watchlist
            # behavior matches pre-Block-23 exactly.
            if not wl_enabled:
                continue

            _handle_watchlist_decision(
                conn,
                candidate=candidate,
                settings=settings,
                report=report,
                now=now,
                base_min_score=base_min_score,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("scoring failed for %s", symbol)
            report.errors.append(f"score {symbol}: {exc}")


def _record_insert_outcome(report: ScanReport, result: InsertResult) -> None:
    """Bump per-reason counters and `inserted_signals` from one InsertResult."""
    report.signal_insert_reasons[result.reason] = (
        report.signal_insert_reasons.get(result.reason, 0) + 1
    )
    if result.inserted:
        report.inserted_signals += 1


def _handle_watchlist_decision(
    conn: sqlite3.Connection,
    *,
    candidate,
    settings: Settings,
    report: ScanReport,
    now: datetime,
    base_min_score: int,
) -> None:
    """Apply the watchlist state machine to a borderline candidate.

    Called only when ``candidate.severity is None`` and the watchlist
    feature is enabled. Updates ``report.watchlist_report`` counters
    and mutates the ``watchlist`` table via the store helpers.
    """
    wl_report = report.watchlist_report
    assert wl_report is not None  # the caller ensures it exists

    has_active = get_watching(conn, symbol=candidate.symbol) is not None
    action = decide_watch_action(
        score=candidate.score,
        min_signal_score=base_min_score,
        floor_score=settings.watchlist.floor_score,
        has_active_watch=has_active,
    )

    if action == WATCH:
        upsert_watching(
            conn,
            symbol=candidate.symbol,
            score=candidate.score,
            now=now,
            max_watch_hours=settings.watchlist.max_watch_hours,
        )
        wl_report.watched += 1
        return

    if action == EXPIRE:
        if expire_below_floor(conn, symbol=candidate.symbol, now=now):
            wl_report.expired_below_floor += 1
        return

    if action == PROMOTE:
        watch = get_watching(conn, symbol=candidate.symbol)
        promoted_severity = _severity_for_score(
            candidate.score, settings.scoring.severity,
        )
        if promoted_severity is None:
            # Defensive: PROMOTE means score >= min_signal_score, and
            # the default config has severity.normal == min_signal_score
            # so this branch shouldn't trigger. If a custom config makes
            # severity.normal > min_signal_score we silently skip.
            return
        promoted_candidate = dataclasses.replace(
            candidate,
            severity=promoted_severity,
            watchlist_id=(watch.id if watch is not None else None),
        )
        result: InsertResult = insert_signal(conn, promoted_candidate)
        _record_insert_outcome(report, result)
        if result.inserted and watch is not None and result.signal_id is not None:
            promote(
                conn,
                symbol=candidate.symbol,
                signal_id=result.signal_id,
                now=now,
            )
            wl_report.promoted += 1
        return

    # IGNORE — nothing to record beyond the counter.
    wl_report.ignored += 1


def _severity_for_score(
    score: int,
    severity_cfg,
) -> str | None:
    """Map a raw score to a severity tier (no min-score gate).

    Used by the PROMOTE branch — the watchlist already established
    that the score warrants a signal, so we skip the emit-floor check
    that ``signals.engine._severity_for`` performs.
    """
    if score >= severity_cfg.very_strong:
        return "very_strong"
    if score >= severity_cfg.strong:
        return "strong"
    if score >= severity_cfg.normal:
        return "normal"
    return None
