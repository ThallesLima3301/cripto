"""CLI entry point and command dispatcher.

Organized as:

  * `build_parser()` — the argparse wiring. Isolated so tests can
    inspect the parser without running anything.
  * `main(argv, ...)` — parses, dispatches, catches top-level
    errors, returns an exit code. Tests call this directly.
  * `_cmd_*` — one handler per subcommand. Each one is a thin
    shim: resolve dependencies (settings, db connection, etc),
    call the relevant lower-layer function, render output.

None of the handlers contains business logic. If you need to add
domain behavior, add it in the appropriate module (buys,
evaluation, reports, scheduler) and have the CLI call it.

Testability
-----------
All handlers take a `_Context` so tests can redirect stdout/stderr.
The scheduler-backed commands (`scan`, `weekly`, `evaluate`) and
`ntfy-test` call functions that tests monkey-patch in place; no
in-process hooks or globals are needed.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Sequence

from crypto_monitor.analytics import (
    compute_expectancy,
    format_expectancy_report,
    load_evaluation_rows,
)
from crypto_monitor.buys import insert_buy, list_buys
from crypto_monitor.config.settings import (
    CONFIG_EXAMPLE_FILENAME,
    CONFIG_FILENAME,
    Settings,
    load_settings,
)
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db, seed_default_symbols
from crypto_monitor.notifications.ntfy import send_ntfy
from crypto_monitor.scheduler import run_maintenance, run_scan, run_weekly
from crypto_monitor.sell import record_sale
from crypto_monitor.utils.time_utils import from_utc_iso, now_utc
from crypto_monitor.watchlist import list_watching


logger = logging.getLogger(__name__)


# ---------- context ----------

@dataclass
class _Context:
    """Shared state for a single CLI invocation.

    Bundling project_root + streams keeps every handler signature
    tidy and lets tests redirect output without touching `sys`.
    """
    project_root: Path
    stdout: IO[str]
    stderr: IO[str]

    def out(self, line: str = "") -> None:
        print(line, file=self.stdout)

    def err(self, line: str) -> None:
        print(line, file=self.stderr)


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser."""
    parser = argparse.ArgumentParser(
        prog="crypto_monitor",
        description="Local-first crypto market analysis and alerting.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
        help="Project root containing config.toml and data/ (default: cwd).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init ----------------------------------------------------------------
    init_p = sub.add_parser(
        "init",
        help="Copy config.example.toml, initialize the database, and seed symbols.",
    )
    init_p.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip symbol seeding even when config.symbols.auto_seed is true.",
    )

    # scan / weekly / evaluate --------------------------------------------
    sub.add_parser(
        "scan",
        help="Run one scan cycle: flush, ingest, score, alert.",
    )
    sub.add_parser(
        "weekly",
        help="Generate and send the weekly summary.",
    )
    sub.add_parser(
        "evaluate",
        help="Evaluate matured signals and buys, prune candles.",
    )

    # buy -----------------------------------------------------------------
    buy_p = sub.add_parser("buy", help="Manage manual buy records.")
    buy_sub = buy_p.add_subparsers(dest="buy_command", required=True)

    buy_add = buy_sub.add_parser("add", help="Record a new buy.")
    buy_add.add_argument("--symbol", required=True, help="Binance pair, e.g. BTCUSDT.")
    buy_add.add_argument("--price", type=float, required=True, help="Execution price.")
    buy_add.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Quote-currency amount invested.",
    )
    buy_add.add_argument(
        "--quote-currency",
        default="USDT",
        help="Quote asset (default: USDT).",
    )
    buy_add.add_argument(
        "--bought-at",
        help="ISO-8601 UTC timestamp of the fill. Defaults to now.",
    )
    buy_add.add_argument(
        "--quantity",
        type=float,
        help="Override derived quantity (base-asset amount).",
    )
    buy_add.add_argument(
        "--signal-id",
        type=int,
        help="Optional signals.id this buy is linked to.",
    )
    buy_add.add_argument("--note", help="Free-form note.")

    buy_list = buy_sub.add_parser("list", help="List recorded buys.")
    buy_list.add_argument("--symbol", help="Filter by symbol.")
    buy_list.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to display (default: 50).",
    )

    # signals -------------------------------------------------------------
    signals_p = sub.add_parser("signals", help="Inspect stored signals.")
    signals_sub = signals_p.add_subparsers(dest="signals_command", required=True)
    signals_list = signals_sub.add_parser("list", help="List recent signals.")
    signals_list.add_argument("--symbol", help="Filter by symbol.")
    signals_list.add_argument(
        "--severity",
        choices=("normal", "strong", "very_strong"),
        help="Filter by severity.",
    )
    signals_list.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to display (default: 50).",
    )

    # sell ----------------------------------------------------------------
    sell_p = sub.add_parser("sell", help="Record manual sales and list sell signals.")
    sell_sub = sell_p.add_subparsers(dest="sell_command", required=True)

    sell_record = sell_sub.add_parser(
        "record",
        help="Mark an open buy as sold (updates the buys row).",
    )
    sell_record.add_argument(
        "--buy-id", type=int, required=True, help="Target buys.id."
    )
    sell_record.add_argument(
        "--price", type=float, required=True, help="Realized sell price."
    )
    sell_record.add_argument(
        "--at",
        dest="sold_at",
        help="ISO-8601 UTC timestamp of the sale. Defaults to now.",
    )
    sell_record.add_argument("--note", help="Free-form note.")

    sell_list = sell_sub.add_parser(
        "list",
        help="List recent sell signals from the sell_signals table.",
    )
    sell_list.add_argument("--symbol", help="Filter by symbol.")
    sell_list.add_argument(
        "--rule",
        choices=(
            "stop_loss", "trailing_stop", "take_profit", "context_deterioration",
        ),
        help="Filter by triggered rule.",
    )
    sell_list.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to display (default: 50).",
    )

    # analytics -----------------------------------------------------------
    analytics_p = sub.add_parser(
        "analytics",
        help="Compute expectancy / win-rate analytics over evaluated signals.",
    )
    analytics_sub = analytics_p.add_subparsers(
        dest="analytics_command", required=True,
    )
    analytics_summary = analytics_sub.add_parser(
        "summary",
        help="Print the expectancy report for a recent window.",
    )
    analytics_summary.add_argument(
        "--scope",
        choices=("all", "90d", "30d"),
        default="all",
        help="Time window for the analytics input (default: all).",
    )
    analytics_summary.add_argument(
        "--min-signals",
        type=int,
        default=5,
        help="Minimum rows for a sliced bucket to appear (default: 5).",
    )

    # watchlist -----------------------------------------------------------
    watchlist_p = sub.add_parser(
        "watchlist",
        help="Inspect borderline-score watchlist entries.",
    )
    watchlist_sub = watchlist_p.add_subparsers(
        dest="watchlist_command", required=True,
    )
    watchlist_sub.add_parser(
        "list",
        help="List active watchlist entries (status='watching').",
    )

    # ntfy-test -----------------------------------------------------------
    ntfy_p = sub.add_parser(
        "ntfy-test",
        help="Send a test notification through the configured ntfy server.",
    )
    ntfy_p.add_argument(
        "--title",
        default="crypto_monitor test",
        help="Notification title.",
    )
    ntfy_p.add_argument(
        "--body",
        default="If you can read this, ntfy is wired up correctly.",
        help="Notification body.",
    )

    return parser


# ---------- entry point ----------

def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the CLI.

    Returns a process exit code: 0 on success, 1 on error, 2 for
    argparse usage errors. `argv` / `stdout` / `stderr` are
    injectable so tests can drive the CLI without touching `sys`.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    ctx = _Context(
        project_root=Path(args.project_root).resolve(),
        stdout=stdout or sys.stdout,
        stderr=stderr or sys.stderr,
    )

    try:
        handler = _HANDLERS[args.command]
        return handler(args, ctx)
    except FileNotFoundError as exc:
        ctx.err(f"error: {exc}")
        return 1
    except ValueError as exc:
        ctx.err(f"error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        logger.exception("cli command failed: %s", args.command)
        ctx.err(f"error: {exc}")
        return 1


# ---------- handlers ----------

def _cmd_init(args: argparse.Namespace, ctx: _Context) -> int:
    """Create config.toml if missing, init the DB, optionally seed symbols."""
    ctx.project_root.mkdir(parents=True, exist_ok=True)

    config_path = ctx.project_root / CONFIG_FILENAME
    example_path = ctx.project_root / CONFIG_EXAMPLE_FILENAME

    if not config_path.exists():
        if not example_path.exists():
            ctx.err(
                f"error: neither {CONFIG_FILENAME} nor {CONFIG_EXAMPLE_FILENAME} "
                f"exists in {ctx.project_root}"
            )
            return 1
        shutil.copyfile(example_path, config_path)
        ctx.out(f"created {config_path}")
    else:
        ctx.out(f"config already exists at {config_path}")

    settings = load_settings(ctx.project_root)

    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        ctx.out(f"initialized database at {settings.general.db_path}")

        seeded = 0
        if not args.no_seed and settings.symbols.auto_seed:
            seeded = seed_default_symbols(conn, list(settings.symbols.tracked))
            ctx.out(f"seeded {seeded} tracked symbol(s)")
        else:
            ctx.out("skipped symbol seeding")
    finally:
        conn.close()

    return 0


def _cmd_scan(args: argparse.Namespace, ctx: _Context) -> int:
    report = run_scan(project_root=ctx.project_root)
    ctx.out(report.summary_line())
    return 0 if not report.errors else 1


def _cmd_weekly(args: argparse.Namespace, ctx: _Context) -> int:
    run = run_weekly(project_root=ctx.project_root)
    ctx.out(
        f"weekly summary id={run.summary_id} "
        f"signals={run.summary.signal_count} "
        f"buys={run.summary.buy_count} "
        f"sent={'yes' if run.send_result.sent else 'no'}"
    )
    return 0 if run.send_result.sent else 1


def _cmd_evaluate(args: argparse.Namespace, ctx: _Context) -> int:
    report = run_maintenance(project_root=ctx.project_root)
    ctx.out(report.summary_line())
    return 0 if not report.errors else 1


# ---------- buy ----------

def _cmd_buy(args: argparse.Namespace, ctx: _Context) -> int:
    if args.buy_command == "add":
        return _cmd_buy_add(args, ctx)
    if args.buy_command == "list":
        return _cmd_buy_list(args, ctx)
    ctx.err(f"error: unknown buy subcommand: {args.buy_command}")
    return 2


def _cmd_buy_add(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    bought_at = _parse_timestamp(args.bought_at) if args.bought_at else now_utc()

    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        record = insert_buy(
            conn,
            symbol=args.symbol,
            bought_at=bought_at,
            price=args.price,
            amount_invested=args.amount,
            quote_currency=args.quote_currency,
            quantity=args.quantity,
            signal_id=args.signal_id,
            note=args.note,
        )
    finally:
        conn.close()

    ctx.out(
        f"recorded buy id={record.id} {record.symbol} "
        f"@ {record.price:g} amount={record.amount_invested:g} "
        f"qty={record.quantity:g} at {record.bought_at}"
    )
    return 0


def _cmd_buy_list(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        records = list_buys(conn, symbol=args.symbol)
    finally:
        conn.close()

    # Show the newest last so the user's eye lands on recent buys
    # without scrolling. list_buys already returns chronological
    # oldest-first, so we slice from the tail.
    if args.limit > 0:
        records = records[-args.limit:]

    if not records:
        ctx.out("(no buys recorded)")
        return 0

    header = f"{'id':>4}  {'symbol':<10}  {'bought_at':<20}  " \
             f"{'price':>10}  {'amount':>10}  {'qty':>12}  " \
             f"{'signal':>6}  note"
    ctx.out(header)
    ctx.out("-" * len(header))
    for r in records:
        ctx.out(
            f"{r.id:>4}  {r.symbol:<10}  {r.bought_at:<20}  "
            f"{r.price:>10.4f}  {r.amount_invested:>10.2f}  "
            f"{r.quantity:>12.6f}  "
            f"{(r.signal_id if r.signal_id is not None else '-'):>6}  "
            f"{r.note or ''}"
        )
    return 0


# ---------- signals ----------

def _cmd_signals(args: argparse.Namespace, ctx: _Context) -> int:
    if args.signals_command == "list":
        return _cmd_signals_list(args, ctx)
    ctx.err(f"error: unknown signals subcommand: {args.signals_command}")
    return 2


def _cmd_signals_list(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        rows = _select_signals(
            conn,
            symbol=args.symbol,
            severity=args.severity,
            limit=max(1, args.limit),
        )
    finally:
        conn.close()

    if not rows:
        ctx.out("(no signals recorded)")
        return 0

    header = f"{'id':>5}  {'detected_at':<20}  {'symbol':<10}  " \
             f"{'sev':<11}  {'score':>5}  {'price':>10}  trigger"
    ctx.out(header)
    ctx.out("-" * len(header))
    for row in rows:
        sev = (row["severity"] or "").replace("_", " ")
        ctx.out(
            f"{row['id']:>5}  {row['detected_at']:<20}  "
            f"{row['symbol']:<10}  {sev:<11}  "
            f"{row['score']:>5}  {row['price_at_signal']:>10.4f}  "
            f"{row['trigger_reason']}"
        )
    return 0


def _select_signals(
    conn: sqlite3.Connection,
    *,
    symbol: str | None,
    severity: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    sql = f"""
        SELECT id, detected_at, symbol, severity, score,
               price_at_signal, trigger_reason
        FROM signals
        {where}
        ORDER BY detected_at DESC, id DESC
        LIMIT ?
    """
    return conn.execute(sql, tuple(params)).fetchall()


# ---------- sell ----------

def _cmd_sell(args: argparse.Namespace, ctx: _Context) -> int:
    if args.sell_command == "record":
        return _cmd_sell_record(args, ctx)
    if args.sell_command == "list":
        return _cmd_sell_list(args, ctx)
    ctx.err(f"error: unknown sell subcommand: {args.sell_command}")
    return 2


def _cmd_sell_record(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    sold_at = _parse_timestamp(args.sold_at) if args.sold_at else now_utc()

    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        run_migrations(conn)  # ensure the sold_* columns exist
        record_sale(
            conn,
            buy_id=args.buy_id,
            sold_at=sold_at,
            sold_price=args.price,
            sold_note=args.note,
        )
    finally:
        conn.close()

    ctx.out(
        f"recorded sale buy_id={args.buy_id} "
        f"price={args.price:g} at {sold_at.isoformat()}"
    )
    return 0


def _cmd_sell_list(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        run_migrations(conn)
        rows = _select_sell_signals(
            conn,
            symbol=args.symbol,
            rule=args.rule,
            limit=max(1, args.limit),
        )
    finally:
        conn.close()

    if not rows:
        ctx.out("(no sell signals recorded)")
        return 0

    header = (
        f"{'id':>5}  {'detected_at':<20}  {'symbol':<10}  "
        f"{'buy':>4}  {'rule':<22}  {'sev':<6}  "
        f"{'price':>10}  {'pnl%':>7}  reason"
    )
    ctx.out(header)
    ctx.out("-" * len(header))
    for row in rows:
        pnl = row["pnl_pct"]
        pnl_txt = f"{pnl:+.2f}" if pnl is not None else "-"
        ctx.out(
            f"{row['id']:>5}  {row['detected_at']:<20}  "
            f"{row['symbol']:<10}  "
            f"{row['buy_id']:>4}  "
            f"{row['rule_triggered']:<22}  "
            f"{row['severity']:<6}  "
            f"{row['price_at_signal']:>10.4f}  "
            f"{pnl_txt:>7}  "
            f"{row['reason']}"
        )
    return 0


def _select_sell_signals(
    conn: sqlite3.Connection,
    *,
    symbol: str | None,
    rule: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if symbol is not None:
        clauses.append("symbol = ?")
        params.append(symbol)
    if rule is not None:
        clauses.append("rule_triggered = ?")
        params.append(rule)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    sql = f"""
        SELECT id, detected_at, symbol, buy_id, rule_triggered,
               severity, price_at_signal, pnl_pct, reason
        FROM sell_signals
        {where}
        ORDER BY detected_at DESC, id DESC
        LIMIT ?
    """
    return conn.execute(sql, tuple(params)).fetchall()


# ---------- analytics ----------

def _cmd_analytics(args: argparse.Namespace, ctx: _Context) -> int:
    if args.analytics_command == "summary":
        return _cmd_analytics_summary(args, ctx)
    ctx.err(f"error: unknown analytics subcommand: {args.analytics_command}")
    return 2


def _cmd_analytics_summary(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        run_migrations(conn)
        rows = load_evaluation_rows(conn, scope=args.scope)
    finally:
        conn.close()

    report = compute_expectancy(rows, min_signals=max(1, args.min_signals))
    ctx.out(format_expectancy_report(report))
    return 0


# ---------- watchlist ----------

def _cmd_watchlist(args: argparse.Namespace, ctx: _Context) -> int:
    if args.watchlist_command == "list":
        return _cmd_watchlist_list(args, ctx)
    ctx.err(f"error: unknown watchlist subcommand: {args.watchlist_command}")
    return 2


def _cmd_watchlist_list(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    conn = get_connection(settings.general.db_path)
    try:
        init_db(conn)
        run_migrations(conn)
        entries = list_watching(conn)
    finally:
        conn.close()

    if not entries:
        ctx.out("(no active watchlist entries)")
        return 0

    header = (
        f"{'id':>4}  {'symbol':<10}  {'first_seen':<20}  "
        f"{'last_seen':<20}  {'score':>5}  {'expires_at':<20}"
    )
    ctx.out(header)
    ctx.out("-" * len(header))
    for e in entries:
        ctx.out(
            f"{e.id:>4}  {e.symbol:<10}  {e.first_seen_at:<20}  "
            f"{e.last_seen_at:<20}  {e.last_score:>5}  {e.expires_at:<20}"
        )
    return 0


# ---------- ntfy-test ----------

def _cmd_ntfy_test(args: argparse.Namespace, ctx: _Context) -> int:
    settings = load_settings(ctx.project_root)
    result = send_ntfy(
        settings.ntfy,
        args.title,
        args.body,
        priority="default",
        tags=("test",),
    )
    if result.sent:
        ctx.out(
            f"sent ok status={result.status_code}"
        )
        return 0

    ctx.err(
        f"ntfy test failed reason={result.reason} "
        f"status={result.status_code} error={result.error}"
    )
    return 1


# ---------- helpers ----------

def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp.

    Accepts the 'Z' suffix the rest of the project uses.
    """
    try:
        return from_utc_iso(value)
    except Exception as exc:  # noqa: BLE001 — narrow re-raise below
        raise ValueError(
            f"could not parse --bought-at {value!r}: {exc}"
        ) from exc


# ---------- dispatch table ----------

_HANDLERS = {
    "init": _cmd_init,
    "scan": _cmd_scan,
    "weekly": _cmd_weekly,
    "evaluate": _cmd_evaluate,
    "buy": _cmd_buy,
    "sell": _cmd_sell,
    "signals": _cmd_signals,
    "watchlist": _cmd_watchlist,
    "analytics": _cmd_analytics,
    "ntfy-test": _cmd_ntfy_test,
}
