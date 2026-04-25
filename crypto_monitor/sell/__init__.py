"""Sell-side data model, persistence, and rule evaluator.

Responsibilities by block:

  * Block 19 — data model + persistence (``SellSignal``, the two
    tables, and the store helpers).
  * Block 20 — pure sell-rule evaluator (``evaluate_sell``). No DB
    access, no side effects.
  * Block 21+ (not yet shipped) — scheduler wiring, watermark
    updates, notification dispatch, and CLI surface.

Nothing in this package decides *when* to run, sends alerts, or
mutates anything outside the sell-specific tables/columns.
"""

from crypto_monitor.sell.engine import PRIORITY_ORDER, evaluate_sell
from crypto_monitor.sell.runtime import ProcessSellReport, process_open_positions
from crypto_monitor.sell.store import (
    get_high_watermark,
    insert_sell_signal,
    last_sell_signal_time,
    load_open_buys,
    record_sale,
    upsert_high_watermark,
)
from crypto_monitor.sell.types import SellSignal

__all__ = [
    "PRIORITY_ORDER",
    "ProcessSellReport",
    "SellSignal",
    "evaluate_sell",
    "get_high_watermark",
    "insert_sell_signal",
    "last_sell_signal_time",
    "load_open_buys",
    "process_open_positions",
    "record_sale",
    "upsert_high_watermark",
]
