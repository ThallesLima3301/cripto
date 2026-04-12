"""SQLite connection factory.

Applies the PRAGMAs required by the rest of the project on every
connection:

  - journal_mode = WAL      (concurrent reads while a scan writes)
  - busy_timeout = 5000 ms  (tolerate brief overlap between scheduled runs)
  - foreign_keys = ON       (enforce references from notifications, buys,
                             buy_evaluations, and signal_evaluations)

Every module that touches the database receives a connection as a
parameter — there are no module-level globals. Tests pass `:memory:`
or a tmp-file connection directly.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def get_connection(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with the project's required PRAGMAs applied.

    `db_path` may be a filesystem path or the string ':memory:' for tests.
    Parent directories are created on demand so the first `cli init` run
    does not have to pre-create `data/`.

    WAL mode is a no-op for in-memory databases; the PRAGMA still returns
    successfully, just with 'memory' as the reported mode.
    """
    path_str = str(db_path)
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        path_str,
        timeout=5.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Commits on clean exit, rolls back on error.

    Useful for one-shot CLI operations. Long-running entry points (scheduler
    scripts) may prefer to hold a single connection and call `conn.commit()`
    at explicit checkpoints instead.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
