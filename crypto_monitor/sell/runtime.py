"""Sell-side runtime orchestrator (Block 21).

Stitches the pieces delivered in Blocks 19 + 20 into the scan cycle:

  1. load open buys (buys with ``sold_at IS NULL``)
  2. for each open buy:
       a. fetch the current price for the buy's symbol
       b. read the *prior* high-watermark from ``sell_tracking``
       c. call :func:`evaluate_sell` with those inputs
       d. if a signal fires **and** the per-(buy, rule) cooldown has
          elapsed, insert the signal and dispatch a notification
       e. *then* update the watermark to ``max(prior, buy.price, current_price)``

The ordering in 2.b → 2.c → 2.e is mandatory: the trailing-stop rule
must see the **prior** peak, never the peak including the current
tick. See ``Block 21`` requirements.

The orchestrator never mutates the buys table (``record_sale`` stays
user-initiated) and never touches the buy-signal notification path.
When ``SellSettings.enabled`` is False the caller skips calling this
module entirely.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from crypto_monitor.buys.manual import BuyRecord
from crypto_monitor.config.settings import NtfySettings, SellSettings
from crypto_monitor.notifications.formatters import (
    SELL_PRIORITY_BY_SEVERITY,
    SELL_PRIORITY_DEFAULT,
    format_sell_alert_body,
    format_sell_alert_title,
)
from crypto_monitor.notifications.ntfy import (
    REASON_SENT,
    SendResult,
    send_ntfy,
)
from crypto_monitor.sell.engine import evaluate_sell
from crypto_monitor.sell.store import (
    get_high_watermark,
    insert_sell_signal,
    last_sell_signal_time,
    load_open_buys,
    upsert_high_watermark,
)
from crypto_monitor.sell.types import SellSignal
from crypto_monitor.utils.time_utils import from_utc_iso, now_utc


logger = logging.getLogger(__name__)


# Injected hooks — tests override these to avoid the network / DB.
NtfySender = Callable[..., SendResult]
PriceLookup = Callable[[sqlite3.Connection, str], float | None]


@dataclass
class ProcessSellReport:
    """Summary of one :func:`process_open_positions` run."""
    considered: int = 0
    evaluated: int = 0
    signals_emitted: int = 0
    signals_sent: int = 0
    signals_send_failed: int = 0
    cooldown_suppressed: int = 0
    no_price: int = 0
    watermarks_touched: int = 0
    errors: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"sell considered={self.considered} evaluated={self.evaluated} "
            f"emitted={self.signals_emitted} sent={self.signals_sent} "
            f"cooldown={self.cooldown_suppressed} no_price={self.no_price} "
            f"watermarks={self.watermarks_touched} errors={len(self.errors)}"
        )


def process_open_positions(
    conn: sqlite3.Connection,
    *,
    settings: SellSettings,
    ntfy: NtfySettings,
    regime_label: str | None = None,
    now: datetime | None = None,
    sender: NtfySender | None = None,
    price_lookup: PriceLookup | None = None,
) -> ProcessSellReport:
    """Run the sell-side pass of a scan cycle.

    Short-circuits to an empty report when ``settings.enabled`` is
    False — the same semantics the regime feature uses, so the call
    site in the scheduler can be unconditional.

    ``price_lookup`` is injected so tests can pin the current price
    without inserting candle rows. The default reads the latest 1h
    candle close for the symbol.
    """
    report = ProcessSellReport()
    if not settings.enabled:
        return report

    if now is None:
        now = now_utc()
    if sender is None:
        sender = send_ntfy
    if price_lookup is None:
        price_lookup = _latest_1h_close

    open_buys = load_open_buys(conn)
    report.considered = len(open_buys)

    cooldown_delta = timedelta(hours=settings.cooldown_hours)

    for buy in open_buys:
        try:
            current_price = price_lookup(conn, buy.symbol)
            if current_price is None or current_price <= 0:
                report.no_price += 1
                continue

            prior_high = get_high_watermark(conn, symbol=buy.symbol, buy_id=buy.id)

            signal = evaluate_sell(
                buy,
                current_price=current_price,
                prior_high_watermark=prior_high,
                regime_label=regime_label,
                settings=settings,
                detected_at=now,
            )
            report.evaluated += 1

            if signal is not None:
                if _cooldown_active(conn, signal, now, cooldown_delta):
                    report.cooldown_suppressed += 1
                else:
                    _persist_and_notify(
                        conn, signal, ntfy, sender, report,
                    )

            # Watermark update runs AFTER evaluation — the trailing-stop
            # rule must see the prior peak. We always pass the max of
            # (buy.price, current_price) so the very first update on a
            # new position never drops the baseline below entry.
            baseline = max(buy.price, current_price)
            if prior_high is None or baseline > prior_high:
                upsert_high_watermark(
                    conn,
                    symbol=buy.symbol,
                    buy_id=buy.id,
                    high_watermark=baseline,
                    now=now,
                )
                report.watermarks_touched += 1

        except Exception as exc:  # noqa: BLE001
            logger.exception("sell pass failed for buy %s", buy.id)
            report.errors.append(f"sell buy {buy.id}: {exc}")

    return report


# ---------- internals ----------

def _cooldown_active(
    conn: sqlite3.Connection,
    signal: SellSignal,
    now: datetime,
    cooldown_delta: timedelta,
) -> bool:
    """True when the same (buy, rule) fired within the cooldown window."""
    if cooldown_delta <= timedelta(0):
        return False
    last_iso = last_sell_signal_time(
        conn,
        buy_id=signal.buy_id,
        rule_triggered=signal.rule_triggered,
    )
    if last_iso is None:
        return False
    last_at = from_utc_iso(last_iso)
    return (now - last_at) < cooldown_delta


def _persist_and_notify(
    conn: sqlite3.Connection,
    signal: SellSignal,
    ntfy: NtfySettings,
    sender: NtfySender,
    report: ProcessSellReport,
) -> None:
    """Insert the signal row, attempt the ntfy send, flip ``alerted``."""
    new_id = insert_sell_signal(conn, signal)
    report.signals_emitted += 1

    row = conn.execute(
        """
        SELECT id, symbol, buy_id, detected_at, price_at_signal,
               rule_triggered, severity, reason, pnl_pct,
               regime_at_signal, alerted
        FROM sell_signals
        WHERE id = ?
        """,
        (new_id,),
    ).fetchone()

    debug = ntfy.debug_notifications
    title = format_sell_alert_title(
        row["symbol"], row["rule_triggered"], debug=debug,
    )
    body = format_sell_alert_body(dict(row), debug=debug)
    priority = SELL_PRIORITY_BY_SEVERITY.get(row["severity"], SELL_PRIORITY_DEFAULT)
    tags = tuple(ntfy.default_tags)

    result = sender(
        ntfy,
        title,
        body,
        priority=priority,
        tags=tags,
    )
    if result.sent and result.reason == REASON_SENT:
        conn.execute(
            "UPDATE sell_signals SET alerted = 1 WHERE id = ?",
            (new_id,),
        )
        conn.commit()
        report.signals_sent += 1
    else:
        # Match the buy-side v1 tradeoff: do not auto-retry. The row
        # stays with alerted=0 so a future cycle could replay it
        # manually if the operator decides to.
        report.signals_send_failed += 1
        logger.warning(
            "sell signal %d send failed: reason=%s error=%s",
            new_id, result.reason, result.error,
        )


def _latest_1h_close(
    conn: sqlite3.Connection,
    symbol: str,
) -> float | None:
    """Default price lookup: latest 1h candle close for ``symbol``.

    Returns ``None`` when no 1h candle exists yet — the caller treats
    this as "no price, skip this buy this cycle" rather than an error.
    """
    row = conn.execute(
        """
        SELECT close FROM candles
        WHERE symbol = ? AND interval = '1h'
        ORDER BY open_time DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return float(row["close"])


__all__ = [
    "ProcessSellReport",
    "process_open_positions",
]
