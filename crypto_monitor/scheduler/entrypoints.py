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
from crypto_monitor.signals.engine import score_signal
from crypto_monitor.signals.persistence import (
    REASON_INSERTED,
    InsertResult,
    insert_signal,
    load_candles,
)
from crypto_monitor.utils.time_utils import now_utc, to_utc_iso

_BTC_REGIME_SYMBOL = "BTCUSDT"


logger = logging.getLogger(__name__)


NtfySender = Callable[..., SendResult]
BinanceClientFactory = Callable[[], BinanceClient]


# ---------- report dataclasses ----------

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
        return (
            f"scan ingest={ingest_total} scored={self.scored_symbols} "
            f"inserted={self.inserted_signals} "
            f"sent={processed} queued={queued} "
            f"cooldown={cd} failed={failed} "
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
        try:
            _score_and_persist(
                conn,
                symbols=scorable,
                settings=settings,
                report=report,
                detected_at=to_utc_iso(now),
                regime=regime,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("scoring pass failed")
            report.errors.append(f"scoring: {exc}")

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
    detected_at: str,
    regime: RegimeSnapshot | None = None,
) -> None:
    """Score every active symbol and persist any new signals.

    This is the one and only place the scheduler stitches the
    (ingested candles) -> (signal engine) -> (dedup insert) pipeline
    together. Per-symbol errors are isolated so a single bad feed
    doesn't abort the whole scan.

    We only need enough history to satisfy the longest lookback the
    engine might touch (180d on the 1d interval, 30d on the 1h
    interval for RSI tail, etc). Loading 250 per interval is the
    same budget `load_candles`'s default uses.
    """
    regime_label = regime.label if regime is not None else None
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
            )
            if candidate is None:
                continue
            report.scored_symbols += 1

            result: InsertResult = insert_signal(conn, candidate)
            report.signal_insert_reasons[result.reason] = (
                report.signal_insert_reasons.get(result.reason, 0) + 1
            )
            if result.inserted:
                report.inserted_signals += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("scoring failed for %s", symbol)
            report.errors.append(f"score {symbol}: {exc}")
