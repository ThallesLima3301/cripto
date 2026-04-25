"""Watchlist subsystem (Block 22 — data model + state machine).

The watchlist parks borderline scores — below the regular buy-signal
emit floor but at or above the configured ``floor_score`` — so they
can be promoted to a real signal if the score climbs past the emit
floor, or quietly aged out if it doesn't.

Block 22 ships only the pieces needed to *remember* watches and
*decide* what a new observation means:

  * ``WatchlistEntry`` — frozen row view of the ``watchlist`` table.
  * ``store``          — SQL helpers: ``upsert_watching``, ``promote``,
                         ``expire_stale``, ``expire_below_floor``,
                         ``get_watching``, ``list_watching``.
  * ``manager``        — pure ``decide_watch_action`` that maps a
                         score + floor + emit threshold + "is there
                         an active watch?" into one of
                         ``WATCH``/``PROMOTE``/``EXPIRE``/``IGNORE``.

Wiring into the scan loop, notifications, and analytics lands in
later blocks — nothing in this package reads candles, inserts
signals, or sends alerts.
"""

from crypto_monitor.watchlist.manager import (
    EXPIRE,
    IGNORE,
    PROMOTE,
    WATCH,
    WATCH_ACTIONS,
    decide_watch_action,
)
from crypto_monitor.watchlist.store import (
    WatchlistEntry,
    expire_below_floor,
    expire_stale,
    get_watching,
    list_watching,
    promote,
    upsert_watching,
)

__all__ = [
    "EXPIRE",
    "IGNORE",
    "PROMOTE",
    "WATCH",
    "WATCH_ACTIONS",
    "WatchlistEntry",
    "decide_watch_action",
    "expire_below_floor",
    "expire_stale",
    "get_watching",
    "list_watching",
    "promote",
    "upsert_watching",
]
