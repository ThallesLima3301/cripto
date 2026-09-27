"""Connection factory protections outside the dashboard thread pool."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from crypto_monitor.database.connection import get_connection


def test_default_connection_remains_bound_to_its_creating_thread():
    """Scanner and CLI callers retain SQLite's default thread guard."""
    conn = get_connection(":memory:")
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
                worker.submit(conn.execute, "SELECT 1").result(timeout=10)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()
