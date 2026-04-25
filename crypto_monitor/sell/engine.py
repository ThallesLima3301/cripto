"""Pure sell-rule evaluator (Block 20).

Given the state of a single open buy and a current market snapshot,
decides whether any sell rule fires and — if so — returns a single
``SellSignal``. That's the entire surface of this block.

What this module intentionally does NOT do
------------------------------------------
* It does **not** touch the database. No reads, no writes. Callers
  load the inputs (buy row, watermark, regime label) from the store
  and pass them in.
* It does **not** persist anything. Watermark updates, sell-signal
  inserts, and buy close-outs are the scheduler's job in Block 21.
* It does **not** send notifications or enforce cooldowns. Those
  live in downstream layers.
* It does **not** read the ``SellSettings.enabled`` kill switch —
  that gate belongs at the call site so the engine stays a pure
  rule evaluator (mirroring how the regime feature flag lives in
  the scheduler, not in ``score_signal``).

Rules
-----
Four rules are checked in a fixed deterministic priority order;
the first match wins:

  1. ``stop_loss``             — ``current_price <= buy.price * (1 - stop_loss_pct/100)``
  2. ``trailing_stop``         — needs a ``prior_high_watermark``;
                                 fires when ``current_price <= hwm * (1 - trailing_stop_pct/100)``
  3. ``take_profit``           — ``current_price >= buy.price * (1 + take_profit_pct/100)``
  4. ``context_deterioration`` — regime label is ``"risk_off"``,
                                 position is at a loss, and
                                 ``settings.context_deterioration`` is True.

Severity mapping
----------------
``stop_loss`` is the only ``"high"`` rule (it represents real
drawdown on the position). Every other rule is ``"medium"``.
Keeping the mapping flat and deterministic makes the downstream
alert path trivial.
"""

from __future__ import annotations

from datetime import datetime

from crypto_monitor.buys.manual import BuyRecord
from crypto_monitor.config.settings import SellSettings
from crypto_monitor.sell.types import SellSignal
from crypto_monitor.utils.time_utils import to_utc_iso


# Documented priority: first match wins. Ordered most-urgent to least.
PRIORITY_ORDER: tuple[str, ...] = (
    "stop_loss",
    "trailing_stop",
    "take_profit",
    "context_deterioration",
)

# Rule -> severity. Flat mapping — see module docstring.
_SEVERITY: dict[str, str] = {
    "stop_loss": "high",
    "trailing_stop": "medium",
    "take_profit": "medium",
    "context_deterioration": "medium",
}


def evaluate_sell(
    buy: BuyRecord,
    *,
    current_price: float,
    prior_high_watermark: float | None,
    regime_label: str | None,
    settings: SellSettings,
    detected_at: datetime,
) -> SellSignal | None:
    """Evaluate sell rules against one open buy; return a ``SellSignal`` or ``None``.

    Arguments
    ---------
    buy                   — the open position we are deciding about.
                            ``buy.price`` is the entry, ``buy.id`` is
                            the FK used on the returned signal.
    current_price         — latest observed price for ``buy.symbol``.
                            Must be > 0.
    prior_high_watermark  — the peak price observed since entry, or
                            ``None`` when the scheduler has not yet
                            tracked one. When ``None`` (or non-positive)
                            the trailing-stop rule is skipped; every
                            other rule still applies.
    regime_label          — current regime label (``"risk_on"`` /
                            ``"neutral"`` / ``"risk_off"``) or ``None``
                            when the regime feature is disabled. Only
                            the context-deterioration rule consumes it;
                            every signal stamps it on
                            ``regime_at_signal`` for analytics.
    settings              — ``SellSettings`` with the four thresholds
                            plus the ``context_deterioration`` flag.
    detected_at           — timezone-aware datetime that will be
                            serialized to UTC ISO 8601 on the signal.

    Returns
    -------
    A ``SellSignal`` when exactly one rule fires, else ``None``. When
    multiple rules would fire simultaneously the engine returns the
    first match in ``PRIORITY_ORDER``.
    """
    if current_price <= 0:
        raise ValueError("current_price must be > 0")
    if detected_at.tzinfo is None:
        raise ValueError("detected_at must be timezone-aware")
    # Defensive — ``insert_buy`` already enforces buy.price > 0; if
    # something corrupted the row we refuse to emit rather than divide
    # by zero.
    if buy.price <= 0:
        return None

    pnl_pct = (current_price - buy.price) / buy.price * 100.0
    detected_iso = to_utc_iso(detected_at)

    # 1. stop_loss
    stop_floor = buy.price * (1.0 - settings.stop_loss_pct / 100.0)
    if current_price <= stop_floor:
        reason = (
            f"stop-loss: price {current_price:.6g} <= {stop_floor:.6g} "
            f"(entry {buy.price:.6g}, pnl {pnl_pct:+.2f}%)"
        )
        return _build_signal(
            buy, "stop_loss", reason,
            current_price, pnl_pct, regime_label, detected_iso,
        )

    # 2. trailing_stop
    if prior_high_watermark is not None and prior_high_watermark > 0:
        trail_floor = prior_high_watermark * (1.0 - settings.trailing_stop_pct / 100.0)
        if current_price <= trail_floor:
            drawdown_pct = (
                (prior_high_watermark - current_price) / prior_high_watermark * 100.0
            )
            reason = (
                f"trailing-stop: -{drawdown_pct:.2f}% from high "
                f"{prior_high_watermark:.6g} (price {current_price:.6g})"
            )
            return _build_signal(
                buy, "trailing_stop", reason,
                current_price, pnl_pct, regime_label, detected_iso,
            )

    # 3. take_profit
    tp_target = buy.price * (1.0 + settings.take_profit_pct / 100.0)
    if current_price >= tp_target:
        reason = (
            f"take-profit: price {current_price:.6g} >= {tp_target:.6g} "
            f"(entry {buy.price:.6g}, pnl {pnl_pct:+.2f}%)"
        )
        return _build_signal(
            buy, "take_profit", reason,
            current_price, pnl_pct, regime_label, detected_iso,
        )

    # 4. context_deterioration
    if (
        settings.context_deterioration
        and regime_label == "risk_off"
        and pnl_pct < 0.0
    ):
        reason = (
            f"regime risk_off with position at {pnl_pct:+.2f}% loss "
            f"(entry {buy.price:.6g}, price {current_price:.6g})"
        )
        return _build_signal(
            buy, "context_deterioration", reason,
            current_price, pnl_pct, regime_label, detected_iso,
        )

    return None


# ---------- internals ----------

def _build_signal(
    buy: BuyRecord,
    rule: str,
    reason: str,
    price: float,
    pnl_pct: float,
    regime_label: str | None,
    detected_iso: str,
) -> SellSignal:
    return SellSignal(
        id=None,
        symbol=buy.symbol,
        buy_id=buy.id,
        detected_at=detected_iso,
        price_at_signal=float(price),
        rule_triggered=rule,
        severity=_SEVERITY[rule],
        reason=reason,
        pnl_pct=float(pnl_pct),
        regime_at_signal=regime_label,
    )
