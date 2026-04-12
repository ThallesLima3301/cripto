"""Signal engine + dedup layer.

Scoring is pure (`engine.score_signal` takes prepared candle data and
returns a `SignalCandidate`); DB insertion and dedup live in
`persistence`. The top-level scan orchestrator that wires ingestion,
scoring, and persistence together is deferred to Block 10.
"""

from crypto_monitor.signals.engine import score_signal
from crypto_monitor.signals.persistence import (
    InsertResult,
    SEVERITY_RANK,
    insert_signal,
    load_candles,
)
from crypto_monitor.signals.types import SignalCandidate

__all__ = [
    "SignalCandidate",
    "score_signal",
    "InsertResult",
    "SEVERITY_RANK",
    "insert_signal",
    "load_candles",
]
