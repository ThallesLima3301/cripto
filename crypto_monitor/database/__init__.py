from crypto_monitor.database.connection import connect, get_connection
from crypto_monitor.database.migrations import (
    BASELINE_VERSION,
    MigrationError,
    MigrationReport,
    run_migrations,
)
from crypto_monitor.database.retention import PruneReport, prune_old_candles, vacuum
from crypto_monitor.database.schema import (
    SCHEMA_VERSION,
    get_schema_version,
    init_db,
    seed_default_symbols,
)

__all__ = [
    "connect",
    "get_connection",
    "init_db",
    "seed_default_symbols",
    "get_schema_version",
    "SCHEMA_VERSION",
    "BASELINE_VERSION",
    "run_migrations",
    "MigrationReport",
    "MigrationError",
    "prune_old_candles",
    "PruneReport",
    "vacuum",
]
