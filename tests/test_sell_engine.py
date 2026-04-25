"""Block 20 — pure sell-rule evaluator.

Every test here drives ``evaluate_sell`` with a hand-built ``BuyRecord``
and a frozen ``SellSettings``. No DB, no fixtures beyond pure Python
values — matching the style of ``test_factors_reversal`` and
``test_factors_v2``.

Coverage:
  * each of the four rules: trigger at the threshold, no-trigger just
    inside the safe zone
  * trailing-stop requires a positive ``prior_high_watermark``
  * context-deterioration respects the regime label, the pnl sign, and
    the config flag
  * priority: stop_loss > trailing_stop > take_profit > context_deterioration
  * pnl_pct correctness for both gains and losses
  * timezone-aware datetime is required
  * ``current_price <= 0`` is rejected
  * deterministic output (same inputs -> same signal)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crypto_monitor.buys.manual import BuyRecord
from crypto_monitor.config.settings import SellSettings
from crypto_monitor.sell import PRIORITY_ORDER, SellSignal, evaluate_sell


UTC = timezone.utc
NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


# ---------- fixtures ----------

def _buy(*, buy_id: int = 1, symbol: str = "BTCUSDT", price: float = 100.0) -> BuyRecord:
    """A minimal BuyRecord — only the fields the evaluator reads matter."""
    return BuyRecord(
        id=buy_id,
        symbol=symbol,
        bought_at="2026-04-20T00:00:00Z",
        price=price,
        amount_invested=1000.0,
        quote_currency="USDT",
        quantity=1000.0 / price,
        signal_id=None,
        note=None,
        created_at="2026-04-20T00:00:00Z",
    )


def _settings(
    *,
    enabled: bool = True,
    stop_loss_pct: float = 8.0,
    take_profit_pct: float = 20.0,
    trailing_stop_pct: float = 10.0,
    context_deterioration: bool = True,
    cooldown_hours: int = 6,
) -> SellSettings:
    return SellSettings(
        enabled=enabled,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        context_deterioration=context_deterioration,
        cooldown_hours=cooldown_hours,
    )


def _call(
    *,
    buy: BuyRecord | None = None,
    current_price: float,
    prior_high_watermark: float | None = None,
    regime_label: str | None = None,
    settings: SellSettings | None = None,
    detected_at: datetime = NOW,
):
    return evaluate_sell(
        buy or _buy(),
        current_price=current_price,
        prior_high_watermark=prior_high_watermark,
        regime_label=regime_label,
        settings=settings or _settings(),
        detected_at=detected_at,
    )


# ---------- stop_loss ----------

class TestStopLoss:

    def test_triggers_at_threshold(self):
        # buy 100, stop 8% -> stop_floor = 92; price at 92 fires.
        sig = _call(current_price=92.0)
        assert sig is not None
        assert sig.rule_triggered == "stop_loss"
        assert sig.severity == "high"
        assert sig.price_at_signal == 92.0

    def test_triggers_below_threshold(self):
        sig = _call(current_price=85.0)
        assert sig is not None
        assert sig.rule_triggered == "stop_loss"

    def test_does_not_trigger_just_above_threshold(self):
        # 92.01 > 92 -> safe zone
        sig = _call(current_price=92.01)
        assert sig is None or sig.rule_triggered != "stop_loss"

    def test_does_not_trigger_when_price_is_above_entry(self):
        sig = _call(current_price=105.0)
        assert sig is None or sig.rule_triggered != "stop_loss"


# ---------- take_profit ----------

class TestTakeProfit:

    def test_triggers_at_threshold(self):
        # buy 100, tp 20% -> target 120; price at 120 fires.
        sig = _call(current_price=120.0)
        assert sig is not None
        assert sig.rule_triggered == "take_profit"
        assert sig.severity == "medium"

    def test_triggers_above_threshold(self):
        sig = _call(current_price=130.0)
        assert sig is not None
        assert sig.rule_triggered == "take_profit"

    def test_does_not_trigger_below_target(self):
        sig = _call(current_price=119.99)
        assert sig is None or sig.rule_triggered != "take_profit"


# ---------- trailing_stop ----------

class TestTrailingStop:

    def test_triggers_when_drop_from_high_meets_threshold(self):
        # high 150, trailing 10% -> trail_floor = 135; price 135 fires.
        sig = _call(current_price=135.0, prior_high_watermark=150.0)
        assert sig is not None
        assert sig.rule_triggered == "trailing_stop"
        assert sig.severity == "medium"

    def test_does_not_trigger_close_to_high(self):
        # 140 > trail_floor 135 -> still in tolerance.
        # TP is widened to 100% here so the 140 price doesn't also trip
        # take_profit and cloud the trailing-stop assertion.
        sig = _call(
            current_price=140.0,
            prior_high_watermark=150.0,
            settings=_settings(take_profit_pct=100.0),
        )
        assert sig is None

    def test_requires_prior_high_watermark(self):
        # No watermark yet — trailing rule cannot fire. Price is in a
        # safe zone for every other rule, so the evaluator returns None.
        sig = _call(current_price=105.0, prior_high_watermark=None)
        assert sig is None

    def test_non_positive_watermark_is_ignored(self):
        sig = _call(current_price=105.0, prior_high_watermark=0.0)
        assert sig is None

    def test_does_not_require_database(self):
        """Sanity: the evaluator is pure — no conn, no I/O."""
        sig = _call(current_price=135.0, prior_high_watermark=150.0)
        assert isinstance(sig, SellSignal)


# ---------- context_deterioration ----------

class TestContextDeterioration:

    def test_triggers_on_risk_off_with_loss_and_flag_enabled(self):
        # price 97 -> pnl -3%, no stop/trail/tp would fire at these thresholds
        sig = _call(
            current_price=97.0,
            regime_label="risk_off",
            settings=_settings(context_deterioration=True),
        )
        assert sig is not None
        assert sig.rule_triggered == "context_deterioration"
        assert sig.severity == "medium"
        assert sig.regime_at_signal == "risk_off"

    def test_does_not_trigger_on_neutral_regime(self):
        sig = _call(current_price=97.0, regime_label="neutral")
        assert sig is None

    def test_does_not_trigger_on_risk_on_regime(self):
        sig = _call(current_price=97.0, regime_label="risk_on")
        assert sig is None

    def test_does_not_trigger_when_regime_is_none(self):
        sig = _call(current_price=97.0, regime_label=None)
        assert sig is None

    def test_does_not_trigger_when_position_is_profitable(self):
        # Gain +5%, but still below take_profit of 20%.
        sig = _call(current_price=105.0, regime_label="risk_off")
        assert sig is None

    def test_does_not_trigger_when_position_is_break_even(self):
        # pnl exactly 0 — rule requires strict loss.
        sig = _call(current_price=100.0, regime_label="risk_off")
        assert sig is None

    def test_does_not_trigger_when_flag_disabled(self):
        sig = _call(
            current_price=97.0,
            regime_label="risk_off",
            settings=_settings(context_deterioration=False),
        )
        assert sig is None


# ---------- priority ----------

class TestPriorityOrder:

    def test_priority_order_constant(self):
        assert PRIORITY_ORDER == (
            "stop_loss",
            "trailing_stop",
            "take_profit",
            "context_deterioration",
        )

    def test_stop_loss_beats_trailing_stop(self):
        # buy 100, stop 10% -> stop_floor 90.
        # high 120, trail 10% -> trail_floor 108.
        # price 85 -> both fire; stop_loss wins.
        sig = _call(
            current_price=85.0,
            prior_high_watermark=120.0,
            settings=_settings(stop_loss_pct=10.0, trailing_stop_pct=10.0),
        )
        assert sig is not None
        assert sig.rule_triggered == "stop_loss"

    def test_trailing_stop_beats_take_profit(self):
        # buy 100, tp 20% -> tp_target 120.
        # high 150, trail 10% -> trail_floor 135.
        # price 125 -> take_profit fires (>=120) AND trailing_stop fires
        # (<=135). Priority: trailing_stop wins.
        sig = _call(
            current_price=125.0,
            prior_high_watermark=150.0,
            settings=_settings(take_profit_pct=20.0, trailing_stop_pct=10.0),
        )
        assert sig is not None
        assert sig.rule_triggered == "trailing_stop"

    def test_stop_loss_beats_context_deterioration(self):
        # Price crashes below stop AND regime is risk_off — stop wins.
        sig = _call(
            current_price=80.0,
            regime_label="risk_off",
            settings=_settings(stop_loss_pct=10.0, context_deterioration=True),
        )
        assert sig is not None
        assert sig.rule_triggered == "stop_loss"

    def test_take_profit_beats_context_deterioration_gains_case(self):
        # Context-deterioration requires a loss, so with a big gain only
        # take_profit can fire — but this still documents the rule
        # ordering for a reader skimming the tests.
        sig = _call(
            current_price=130.0,
            regime_label="risk_off",
            settings=_settings(take_profit_pct=20.0),
        )
        assert sig is not None
        assert sig.rule_triggered == "take_profit"


# ---------- pnl_pct ----------

class TestPnlPct:

    def test_positive_gain(self):
        sig = _call(current_price=120.0)  # +20% gain -> take_profit fires
        assert sig is not None
        assert sig.pnl_pct == pytest.approx(20.0)

    def test_negative_loss(self):
        sig = _call(current_price=90.0)   # -10% loss -> stop_loss fires
        assert sig is not None
        assert sig.pnl_pct == pytest.approx(-10.0)

    def test_pnl_uses_entry_price(self):
        buy = _buy(price=50.0)
        sig = _call(buy=buy, current_price=45.0)  # -10% loss
        assert sig is not None
        assert sig.pnl_pct == pytest.approx(-10.0)


# ---------- time safety and validation ----------

class TestValidation:

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            evaluate_sell(
                _buy(),
                current_price=95.0,
                prior_high_watermark=None,
                regime_label=None,
                settings=_settings(),
                detected_at=datetime(2026, 4, 23, 12, 0),  # naive
            )

    def test_rejects_non_positive_current_price(self):
        for bad in (0.0, -1.0):
            with pytest.raises(ValueError, match="current_price"):
                _call(current_price=bad)

    def test_detected_at_serialized_as_utc_iso(self):
        sig = _call(current_price=90.0)
        assert sig is not None
        assert sig.detected_at == "2026-04-23T12:00:00Z"

    def test_returns_none_when_no_rule_fires(self):
        # Price +5%, no risk-off regime, no watermark -> silence.
        sig = _call(current_price=105.0)
        assert sig is None


# ---------- full signal shape ----------

class TestSignalShape:

    def test_populated_fields_match_schema(self):
        sig = _call(
            current_price=90.0,
            regime_label="risk_off",
        )
        assert sig is not None
        # Identity
        assert sig.id is None
        assert sig.symbol == "BTCUSDT"
        assert sig.buy_id == 1
        # Time + price
        assert sig.detected_at == "2026-04-23T12:00:00Z"
        assert sig.price_at_signal == 90.0
        # Rule + severity + reason
        assert sig.rule_triggered == "stop_loss"
        assert sig.severity == "high"
        assert "stop-loss" in sig.reason
        # P&L + regime stamp
        assert sig.pnl_pct == pytest.approx(-10.0)
        assert sig.regime_at_signal == "risk_off"
        # Pre-insert state
        assert sig.alerted == 0

    def test_deterministic_output(self):
        """Identical inputs produce identical signals."""
        a = _call(current_price=95.0, prior_high_watermark=110.0,
                  regime_label="risk_off")
        b = _call(current_price=95.0, prior_high_watermark=110.0,
                  regime_label="risk_off")
        assert a == b
