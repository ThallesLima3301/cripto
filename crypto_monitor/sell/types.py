"""Shared types for the sell package."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SellSignal:
    """One row from the ``sell_signals`` table.

    Stored append-only — every fired sell rule produces a new row, even
    when the same buy fires the same rule twice (the cooldown is enforced
    upstream, not at the schema level, so the log retains every event).

    Fields
    ------
    id               — autoincrement primary key. ``None`` before insert.
    symbol           — Binance pair, e.g. ``"BTCUSDT"``.
    buy_id           — FK into ``buys.id``. The position this signal
                       refers to.
    detected_at      — UTC ISO 8601 timestamp the signal was raised.
    price_at_signal  — close price observed when the rule fired.
    rule_triggered   — short tag identifying the rule (e.g.
                       ``"stop_loss"``, ``"take_profit"``,
                       ``"trailing_stop"``, ``"context_deterioration"``).
                       Free TEXT so future rules can be added without
                       a schema migration.
    severity         — short tier label for the alert path
                       (``"info" | "warn" | "critical"``).
    reason           — human-readable summary suitable for the
                       notification body.
    pnl_pct          — realized-vs-entry P&L in percent at the moment of
                       the signal. Optional — some rules (context-only)
                       may leave it ``None``.
    regime_at_signal — regime label active when the signal fired, when
                       the regime feature is enabled. ``None`` otherwise.
    alerted          — 0 = not yet dispatched, 1 = sent. Block 19 only
                       initializes the column; the alert pipeline
                       toggles it later.
    """

    id: int | None
    symbol: str
    buy_id: int
    detected_at: str
    price_at_signal: float
    rule_triggered: str
    severity: str
    reason: str
    pnl_pct: float | None = None
    regime_at_signal: str | None = None
    alerted: int = 0
