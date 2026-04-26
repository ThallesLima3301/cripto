"""Read-only dashboard adapter (Step 1 of the dashboard architecture).

The dashboard layer is a thin FastAPI app that translates existing
domain readers (``buys.list_buys``, ``watchlist.store.list_watching``,
``regime.store.load_latest_regime``, ``analytics.compute_expectancy``,
…) into stable JSON responses. The bot's modules never learn that a
dashboard exists; the dashboard never executes SQL that doesn't
already live in (or belong in) one of the existing ``*/store.py``
readers.

Step 1 ships:

  * ``api.app``      — the FastAPI application object.
  * ``deps``         — request-scoped DB connection dependency.
  * ``schemas``      — Pydantic response models (the stable wire
                       contract that decouples the frontend from
                       internal dataclass shapes).
  * ``services``     — per-endpoint composition of reader calls into
                       schema instances.

Importing this package does **not** start a server and does **not**
open a database connection; both happen on first request.
"""

from crypto_monitor.dashboard.api import app

__all__ = ["app"]
