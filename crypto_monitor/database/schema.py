"""SQLite schema definition and initialization.

Contains every CREATE TABLE and CREATE INDEX statement the application
needs plus an `init_db()` function that runs them idempotently.

Schema version is tracked in a small `schema_meta` table so future
migrations can check the current version and apply deltas without
guessing. For v1 there is only one version; migrations will become
real code only when a second version is needed.

v1 price-type tradeoff
----------------------
Prices and amounts are stored as REAL (Python float). This is
intentional for v1 because:

  * we care about signal detection and percentage movement, not
    exact portfolio accounting;
  * the tracked quote assets (USDT-class stablecoins) have at most 8
    decimals of precision, well within float64 range for the values
    we deal with;
  * float keeps the code trivial and removes a class of cross-library
    friction (SQLite has no native DECIMAL type).

If this app later grows into real portfolio management, prices should
migrate to TEXT-stored Decimal. That is a schema bump; `schema_meta`
below is the mechanism by which the migration would be applied.
"""

from __future__ import annotations

import sqlite3

from crypto_monitor.utils.time_utils import now_utc, to_utc_iso


SCHEMA_VERSION = 1


# ---------- table DDL ----------

_CREATE_STATEMENTS: tuple[str, ...] = (
    # Schema metadata (version + first-init timestamp + future keys).
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,

    # Tracked symbols.
    """
    CREATE TABLE IF NOT EXISTS symbols (
        id          INTEGER PRIMARY KEY,
        symbol      TEXT NOT NULL UNIQUE,
        base        TEXT NOT NULL,
        quote       TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1,
        added_at    TEXT NOT NULL
    )
    """,

    # Closed OHLCV candles, multi-interval.
    """
    CREATE TABLE IF NOT EXISTS candles (
        id          INTEGER PRIMARY KEY,
        symbol      TEXT NOT NULL,
        interval    TEXT NOT NULL,
        open_time   TEXT NOT NULL,
        open        REAL NOT NULL,
        high        REAL NOT NULL,
        low         REAL NOT NULL,
        close       REAL NOT NULL,
        volume      REAL NOT NULL,
        close_time  TEXT NOT NULL,
        UNIQUE(symbol, interval, open_time)
    )
    """,

    # Generated signals. The UNIQUE (symbol, candle_hour, severity)
    # constraint enforces the dedup rule from Phase 1: at most one
    # signal row per symbol per closed 1h candle, with a severity
    # escalation exception (same candle, higher severity = allowed).
    """
    CREATE TABLE IF NOT EXISTS signals (
        id                              INTEGER PRIMARY KEY,
        symbol                          TEXT NOT NULL,
        detected_at                     TEXT NOT NULL,
        candle_hour                     TEXT NOT NULL,
        price_at_signal                 REAL NOT NULL,
        score                           INTEGER NOT NULL,
        severity                        TEXT NOT NULL,
        trigger_reason                  TEXT NOT NULL,
        dominant_trigger_timeframe      TEXT,
        drop_1h_pct                     REAL,
        drop_24h_pct                    REAL,
        drop_7d_pct                     REAL,
        drop_30d_pct                    REAL,
        drop_180d_pct                   REAL,
        distance_from_30d_high_pct      REAL,
        distance_from_180d_high_pct     REAL,
        recent_30d_high                 REAL,
        recent_180d_high                REAL,
        drop_trigger_pct                REAL,
        rsi_1h                          REAL,
        rsi_4h                          REAL,
        rel_volume                      REAL,
        dist_support_pct                REAL,
        support_level_price             REAL,
        reversal_signal                 INTEGER NOT NULL DEFAULT 0,
        trend_context_4h                TEXT,
        trend_context_1d                TEXT,
        score_breakdown                 TEXT NOT NULL,
        alerted                         INTEGER NOT NULL DEFAULT 0,
        alert_skipped_reason            TEXT,
        UNIQUE(symbol, candle_hour, severity)
    )
    """,

    # Notifications. This table also serves as the pending-delivery queue
    # for quiet-hour alerts (Phase 1 point 8): rows with delivered=0 AND
    # queued=1 are the outstanding queue, rows with delivered=1 are
    # history. A single table is sufficient for v1 volume.
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id                  INTEGER PRIMARY KEY,
        created_at          TEXT NOT NULL,
        sent_at             TEXT,
        symbol              TEXT,
        signal_id           INTEGER REFERENCES signals(id),
        title               TEXT NOT NULL,
        body                TEXT NOT NULL,
        priority            TEXT NOT NULL,
        tags                TEXT,
        queued              INTEGER NOT NULL DEFAULT 0,
        bypass_quiet        INTEGER NOT NULL DEFAULT 0,
        delivered           INTEGER NOT NULL DEFAULT 0,
        delivery_attempts   INTEGER NOT NULL DEFAULT 0,
        last_error          TEXT
    )
    """,

    # Manual buy records.
    """
    CREATE TABLE IF NOT EXISTS buys (
        id                  INTEGER PRIMARY KEY,
        symbol              TEXT NOT NULL,
        bought_at           TEXT NOT NULL,
        price               REAL NOT NULL,
        amount_invested     REAL NOT NULL,
        quote_currency      TEXT NOT NULL,
        quantity            REAL,
        signal_id           INTEGER REFERENCES signals(id),
        note                TEXT,
        created_at          TEXT NOT NULL
    )
    """,

    # Buy evaluations. Hourly-resolution wording is explicit in column
    # names to honor Phase 1 point 6.
    """
    CREATE TABLE IF NOT EXISTS buy_evaluations (
        id                              INTEGER PRIMARY KEY,
        buy_id                          INTEGER NOT NULL UNIQUE REFERENCES buys(id),
        evaluated_at                    TEXT NOT NULL,
        day_open                        REAL,
        day_low_hourly                  REAL,
        day_low_hourly_time             TEXT,
        pct_from_day_open_to_low_hourly REAL,
        pct_from_buy_to_low_hourly      REAL,
        buy_vs_day_low_hourly_pct       REAL,
        price_7d_later                  REAL,
        return_7d_pct                   REAL,
        price_30d_later                 REAL,
        return_30d_pct                  REAL,
        verdict                         TEXT,
        resolution_note                 TEXT
    )
    """,

    # Signal evaluations.
    """
    CREATE TABLE IF NOT EXISTS signal_evaluations (
        id                  INTEGER PRIMARY KEY,
        signal_id           INTEGER NOT NULL UNIQUE REFERENCES signals(id),
        evaluated_at        TEXT NOT NULL,
        price_at_signal     REAL NOT NULL,
        price_24h_later     REAL,
        price_7d_later      REAL,
        price_30d_later     REAL,
        return_24h_pct      REAL,
        return_7d_pct       REAL,
        return_30d_pct      REAL,
        max_gain_7d_pct     REAL,
        max_loss_7d_pct     REAL,
        verdict             TEXT
    )
    """,

    # Weekly summaries.
    """
    CREATE TABLE IF NOT EXISTS weekly_summaries (
        id              INTEGER PRIMARY KEY,
        week_start      TEXT NOT NULL,
        week_end        TEXT NOT NULL,
        generated_at    TEXT NOT NULL,
        body            TEXT NOT NULL,
        signal_count    INTEGER,
        buy_count       INTEGER,
        top_drop_symbol TEXT,
        top_drop_pct    REAL,
        sent            INTEGER NOT NULL DEFAULT 0
    )
    """,

    # Processing state (last candle ingested per symbol/interval, etc).
    """
    CREATE TABLE IF NOT EXISTS processing_state (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
)


# ---------- indexes ----------

_CREATE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_time "
    "ON candles(symbol, interval, open_time)",
    "CREATE INDEX IF NOT EXISTS idx_candles_close_time ON candles(close_time)",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol_detected "
    "ON signals(symbol, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_signals_candle_hour "
    "ON signals(symbol, candle_hour)",
    "CREATE INDEX IF NOT EXISTS idx_signals_alerted "
    "ON signals(alerted, detected_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_pending "
    "ON notifications(delivered, queued)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_signal "
    "ON notifications(signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_buys_symbol ON buys(symbol, bought_at)",
    "CREATE INDEX IF NOT EXISTS idx_buy_eval_buy ON buy_evaluations(buy_id)",
    "CREATE INDEX IF NOT EXISTS idx_signal_eval_signal "
    "ON signal_evaluations(signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_weekly_summaries_week "
    "ON weekly_summaries(week_start)",
)


# ---------- public functions ----------

def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes idempotently and record schema version.

    Safe to call on every run — every statement uses IF NOT EXISTS. The
    schema_meta row is upserted so the `updated_at` is refreshed even when
    nothing changed, which is useful for auditing.
    """
    for stmt in _CREATE_STATEMENTS:
        conn.execute(stmt)
    for stmt in _CREATE_INDEXES:
        conn.execute(stmt)
    _upsert_schema_meta(conn)
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the currently stored schema version, or None if uninitialized."""
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def seed_default_symbols(conn: sqlite3.Connection, symbols: list[str]) -> int:
    """Insert tracked symbols into the `symbols` table if missing.

    `symbols` is a list of Binance pairs like ['BTCUSDT', 'ETHUSDT'].
    Returns the number of rows actually inserted.
    """
    inserted = 0
    ts = to_utc_iso(now_utc())
    for sym in symbols:
        base, quote = _split_symbol(sym)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO symbols (symbol, base, quote, active, added_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (sym, base, quote, ts),
        )
        if cur.rowcount > 0:
            inserted += cur.rowcount
    conn.commit()
    return inserted


# ---------- internals ----------

def _upsert_schema_meta(conn: sqlite3.Connection) -> None:
    ts = to_utc_iso(now_utc())
    conn.execute(
        """
        INSERT INTO schema_meta (key, value, updated_at)
        VALUES ('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (str(SCHEMA_VERSION), ts),
    )
    conn.execute(
        """
        INSERT INTO schema_meta (key, value, updated_at)
        VALUES ('initialized_at', ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (ts, ts),
    )


_KNOWN_QUOTES: tuple[str, ...] = (
    "USDT", "BUSD", "USDC", "FDUSD", "TUSD",
    "EUR", "TRY", "BRL",
    "BTC", "ETH", "BNB",
)


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Split a Binance pair like BTCUSDT into (base, quote).

    Tries a known-quote suffix match first. Falls back to a 3-character
    base split, which covers most remaining Binance spot pairs.
    """
    for quote in _KNOWN_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol[:3], symbol[3:]
