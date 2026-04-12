"""Manual buy logging.

Block 8 owns the single path by which a user records that they've
bought a coin. It is deliberately thin:

  * `insert_buy` writes a row to the `buys` table with an optional
    link to the signal that motivated the purchase.
  * `BuyRecord` is the frozen dataclass round-tripped by the insert
    and the few read helpers used by Block 8's evaluator.

No automatic buys, no portfolio math, no order sizing — any of that
is well outside Phase 1 scope.
"""

from crypto_monitor.buys.manual import (
    BuyRecord,
    get_buy,
    insert_buy,
    list_buys,
)

__all__ = [
    "BuyRecord",
    "insert_buy",
    "get_buy",
    "list_buys",
]
