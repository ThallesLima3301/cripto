"""FastAPI dependencies for the dashboard adapter.

All DB connection lifecycle and settings resolution lives here so the
route modules stay focused on the HTTP layer.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from crypto_monitor.config.settings import Settings, load_settings
from crypto_monitor.database.connection import get_connection


# Project root used to resolve config + DB paths. Production hosts can
# override via env (``CRYPTO_MONITOR_PROJECT_ROOT``) without changing
# code; tests replace settings resolution with a temporary DB path.
def _resolve_project_root() -> Path:
    import os
    raw = os.environ.get("CRYPTO_MONITOR_PROJECT_ROOT")
    if raw:
        return Path(raw).resolve()
    return Path.cwd().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached :class:`Settings` instance for the running process.

    The dashboard is a single-process server; loading the TOML once
    and reusing it removes a small but real per-request hit.
    """
    return load_settings(_resolve_project_root())


def get_db(settings: Settings = None) -> Iterator[sqlite3.Connection]:  # type: ignore[assignment]
    """Yield a per-request SQLite connection using project pragmas.

    Opened with the same pragma stack the bot itself uses (WAL mode,
    busy_timeout, foreign keys), so the API can read concurrently
    while a scheduler-driven scan writes. Closed in ``finally`` even
    on exceptions.

    FastAPI may run dependency setup, the synchronous handler, and
    cleanup on different worker threads. Allow that handoff while
    keeping one connection per request, used sequentially by its
    handler and never shared with another request.

    If no settings instance is provided, resolve the cached settings
    from :func:`get_settings`.
    """
    if settings is None:
        settings = get_settings()
    conn = get_connection(settings.general.db_path, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()
