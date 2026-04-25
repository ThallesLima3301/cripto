"""Tests for `crypto_monitor.notifications.formatters`.

The formatters module is pure: no DB, no I/O. Tests live at the
level of "given this structured input, the returned string contains
these markers". We do not pin the exact whitespace-by-whitespace
output so future phrasing tweaks don't require reshuffling every
test.
"""

from __future__ import annotations

import pytest

from crypto_monitor.notifications.formatters import (
    SELL_PRIORITY_BY_SEVERITY,
    SELL_PRIORITY_DEFAULT,
    format_alert_body,
    format_alert_title,
    format_sell_alert_body,
    format_sell_alert_title,
    format_weekly_body,
    format_weekly_title,
    friendly_name,
)


# ---------- friendly_name ----------

def test_friendly_name_known_pairs():
    assert friendly_name("BTCUSDT") == "BTC"
    assert friendly_name("ETHUSDT") == "ETH"
    assert friendly_name("SOLUSDT") == "SOL"


def test_friendly_name_unknown_usdt_strips_suffix():
    # Unknown USDT pair: strip the trailing USDT.
    assert friendly_name("PEPEUSDT") == "PEPE"


def test_friendly_name_unknown_non_usdt_returns_raw():
    assert friendly_name("BTCBRL") == "BTCBRL"
    assert friendly_name("USDT") == "USDT"  # too short to strip


# ---------- format_alert_title ----------

def test_alert_title_very_strong_uses_green_decision():
    title = format_alert_title("BTCUSDT", "very_strong")
    assert "🟢" in title
    assert "Bom momento de compra" in title
    assert "BTC" in title
    # Client mode omits the raw pair.
    assert "BTCUSDT" not in title


def test_alert_title_strong_uses_yellow_decision():
    title = format_alert_title("ETHUSDT", "strong")
    assert "🟡" in title
    assert "Vale observar" in title
    assert "ETH" in title


def test_alert_title_normal_uses_orange_decision():
    title = format_alert_title("SOLUSDT", "normal")
    assert "🟠" in title
    assert "Melhor esperar" in title
    assert "SOL" in title


def test_alert_title_unknown_severity_falls_back_to_red():
    title = format_alert_title("BTCUSDT", "bogus")
    assert "🔴" in title
    assert "Evitar entrada agora" in title


def test_alert_title_debug_appends_raw_pair():
    title = format_alert_title("BTCUSDT", "very_strong", debug=True)
    assert "BTC" in title
    assert "(BTCUSDT)" in title


# ---------- format_alert_body ----------

def _base_row(**overrides):
    """Minimal signal row with sensible defaults."""
    row = {
        "symbol": "BTCUSDT",
        "severity": "very_strong",
        "price_at_signal": 42000.0,
        "score": 85,
        "trigger_reason": "drop_24h",
        "dominant_trigger_timeframe": "24h",
        "drop_trigger_pct": 8.2,
        "rsi_1h": 18.0,
        "rsi_4h": 30.0,
        "rel_volume": 2.5,
        "drop_24h_pct": 8.2,
        "drop_7d_pct": 12.0,
        "drop_30d_pct": 18.0,
        "dist_support_pct": 1.2,
        "support_level_price": 41000.0,
        "distance_from_30d_high_pct": 15.0,
        "distance_from_180d_high_pct": 35.0,
        "reversal_pattern": "hammer",
        "regime_at_signal": "risk_on",
    }
    row.update(overrides)
    return row


def test_alert_body_client_mode_contains_price_and_variation():
    body = format_alert_body(_base_row())
    assert "BTC @ $42000" in body
    assert "Variação 24h: -8.2%" in body


def test_alert_body_client_mode_contains_top_reasons():
    body = format_alert_body(_base_row())
    # Drop reason should always appear (priority 1).
    assert "Queda de 8.2%" in body
    assert "24 horas" in body
    # Extreme RSI (priority 2).
    assert "sobrevenda extrema" in body


def test_alert_body_client_mode_caps_reasons_at_three():
    body = format_alert_body(_base_row())
    bullets = [line for line in body.splitlines() if line.startswith("• ")]
    assert len(bullets) == 3


def test_alert_body_client_mode_interpretation_at_end():
    body = format_alert_body(_base_row())
    assert "Vários indicadores apontam oportunidade clara." in body


def test_alert_body_client_mode_regime_line_when_non_neutral():
    body = format_alert_body(_base_row(regime_at_signal="risk_on"))
    assert "risk-on" in body


def test_alert_body_client_mode_regime_neutral_is_hidden():
    body = format_alert_body(_base_row(regime_at_signal="neutral"))
    assert "risk-on" not in body
    assert "risk-off" not in body


def test_alert_body_client_mode_omits_raw_pair():
    body = format_alert_body(_base_row())
    assert "BTCUSDT" not in body


def test_alert_body_rsi_borderline_not_extreme():
    # RSI=28 is oversold but not extreme — should say "sobrevenda"
    # without "extrema".
    body = format_alert_body(_base_row(rsi_1h=28.0))
    assert "sobrevenda" in body
    assert "extrema" not in body


def test_alert_body_pattern_translated():
    body = format_alert_body(
        _base_row(
            # Push drop off the top so the pattern reason makes it in.
            drop_trigger_pct=None,
            dominant_trigger_timeframe=None,
            reversal_pattern="bullish_engulfing",
        )
    )
    assert "engolfo de alta" in body


def test_alert_body_minimal_row_still_renders():
    # Row with everything optional missing: only the required fields
    # are present. Body should still render without errors.
    body = format_alert_body({
        "symbol": "ETHUSDT",
        "severity": "normal",
        "price_at_signal": 3500.0,
    })
    assert "ETH @ $3500" in body
    assert "Melhor esperar" not in body  # that's the title
    # The interpretation for normal severity IS included.
    assert "Sinal fraco" in body


def test_alert_body_debug_mode_appends_raw_block():
    body = format_alert_body(_base_row(), debug=True)
    assert "--- debug ---" in body
    assert "pair=BTCUSDT" in body
    assert "rsi_1h=18" in body
    assert "regime=risk_on" in body


def test_alert_body_debug_mode_still_has_client_text():
    body = format_alert_body(_base_row(), debug=True)
    # Client text comes first.
    assert body.index("BTC @") < body.index("--- debug ---")


# ---------- format_weekly_title ----------

def test_weekly_title_client_uses_dd_mm():
    title = format_weekly_title(
        "2026-04-04T12:00:00Z", "2026-04-11T12:00:00Z"
    )
    assert "04/04" in title
    assert "11/04" in title
    assert "Resumo semanal" in title
    # Client mode hides the ISO detail.
    assert "2026-04-04" not in title


def test_weekly_title_debug_appends_iso_range():
    title = format_weekly_title(
        "2026-04-04T12:00:00Z", "2026-04-11T12:00:00Z", debug=True
    )
    assert "04/04" in title
    assert "2026-04-04" in title
    assert "2026-04-11" in title


# ---------- format_weekly_body ----------

def _weekly_kwargs(**overrides):
    """Default weekly body kwargs for an average busy week."""
    kwargs = dict(
        week_start_iso="2026-04-04T12:00:00Z",
        week_end_iso="2026-04-11T12:00:00Z",
        signal_count=3,
        signal_by_severity={"very_strong": 1, "strong": 2},
        top_drop_symbol="BTCUSDT",
        top_drop_pct=8.2,
        buy_count=1,
        matured_count=2,
        verdict_counts={"great": 1, "bad": 1},
    )
    kwargs.update(overrides)
    return kwargs


def test_weekly_body_contains_header_and_counts():
    body = format_weekly_body(**_weekly_kwargs())
    assert "📊 Resumo da semana" in body
    assert "Sinais emitidos: 3" in body
    assert "Críticos: 1" in body
    assert "Fortes: 2" in body


def test_weekly_body_contains_friendly_top_drop():
    body = format_weekly_body(**_weekly_kwargs())
    assert "Maior queda: BTC (-8.2%)" in body


def test_weekly_body_buys_section_only_when_positive():
    with_buys = format_weekly_body(**_weekly_kwargs(buy_count=3))
    assert "Compras registradas: 3" in with_buys

    no_buys = format_weekly_body(**_weekly_kwargs(buy_count=0))
    assert "Compras registradas" not in no_buys


def test_weekly_body_verdicts_section_only_when_matured():
    no_matured = format_weekly_body(
        **_weekly_kwargs(matured_count=0, verdict_counts={})
    )
    assert "Avaliações vencidas" not in no_matured

    with_matured = format_weekly_body(**_weekly_kwargs())
    assert "Avaliações vencidas: 2" in with_matured
    assert "Boas: 1" in with_matured
    assert "Ruins: 1" in with_matured


def test_weekly_body_verdict_grouping():
    body = format_weekly_body(
        **_weekly_kwargs(
            matured_count=6,
            verdict_counts={
                "great": 2, "good": 1,       # grouped as "Boas"
                "neutral": 1,                # "Neutras"
                "poor": 1, "bad": 1,         # grouped as "Ruins"
            },
        )
    )
    assert "Boas: 3" in body
    assert "Neutras: 1" in body
    assert "Ruins: 2" in body


def test_weekly_body_empty_week_uses_tranquila_marker():
    body = format_weekly_body(
        **_weekly_kwargs(
            signal_count=0,
            signal_by_severity={},
            top_drop_symbol=None,
            top_drop_pct=None,
            buy_count=0,
            matured_count=0,
            verdict_counts={},
        )
    )
    assert "semana tranquila" in body
    # No top-drop line when there was no drop.
    assert "Maior queda" not in body


def test_weekly_body_always_ends_with_conclusion():
    body = format_weekly_body(**_weekly_kwargs())
    assert "Leitura rápida:" in body


def test_weekly_conclusion_very_strong_flavor():
    body = format_weekly_body(
        **_weekly_kwargs(
            signal_count=1,
            signal_by_severity={"very_strong": 1},
            top_drop_symbol="BTCUSDT",
            top_drop_pct=9.0,
        )
    )
    assert "sinal forte de compra" in body


def test_weekly_conclusion_strong_only_flavor():
    body = format_weekly_body(
        **_weekly_kwargs(
            signal_count=2,
            signal_by_severity={"strong": 2},
        )
    )
    assert "sinais moderados" in body


def test_weekly_conclusion_normal_only_flavor():
    body = format_weekly_body(
        **_weekly_kwargs(
            signal_count=1,
            signal_by_severity={"normal": 1},
        )
    )
    assert "sinais fracos" in body


def test_weekly_conclusion_empty_week_flavor():
    body = format_weekly_body(
        **_weekly_kwargs(
            signal_count=0,
            signal_by_severity={},
            top_drop_symbol=None,
            top_drop_pct=None,
            buy_count=0,
            matured_count=0,
            verdict_counts={},
        )
    )
    assert "sem oportunidades" in body


def test_weekly_body_debug_appends_raw_block():
    body = format_weekly_body(**_weekly_kwargs(debug=True))
    assert "--- debug ---" in body
    assert "signal_count=3" in body
    assert "top_drop=BTCUSDT" in body
    assert "matured=2" in body


# ---------- sell formatter (Block 21) ----------

def _sell_row(
    *,
    symbol: str = "BTCUSDT",
    rule: str = "stop_loss",
    severity: str = "high",
    price: float = 85.0,
    pnl: float | None = -15.0,
    regime: str | None = "risk_off",
    reason: str = "stop-loss: price 85 <= 92 (entry 100, pnl -15.00%)",
    buy_id: int = 1,
    detected_at: str = "2026-04-23T15:00:00Z",
) -> dict:
    return {
        "id": 1,
        "symbol": symbol,
        "buy_id": buy_id,
        "detected_at": detected_at,
        "price_at_signal": price,
        "rule_triggered": rule,
        "severity": severity,
        "reason": reason,
        "pnl_pct": pnl,
        "regime_at_signal": regime,
        "alerted": 0,
    }


class TestSellAlertTitle:

    def test_stop_loss_title(self):
        t = format_sell_alert_title("BTCUSDT", "stop_loss")
        assert "Stop-loss" in t
        assert "BTC" in t

    def test_take_profit_title(self):
        t = format_sell_alert_title("ETHUSDT", "take_profit")
        assert "Take-profit" in t
        assert "ETH" in t

    def test_trailing_stop_title(self):
        t = format_sell_alert_title("SOLUSDT", "trailing_stop")
        assert "Trailing stop" in t or "Trailing" in t
        assert "SOL" in t

    def test_context_deterioration_title(self):
        t = format_sell_alert_title("BTCUSDT", "context_deterioration")
        assert "Contexto" in t

    def test_unknown_rule_uses_fallback(self):
        t = format_sell_alert_title("BTCUSDT", "brand_new_rule")
        assert "venda" in t.lower()

    def test_debug_appends_pair_and_rule(self):
        t = format_sell_alert_title("BTCUSDT", "stop_loss", debug=True)
        assert "BTCUSDT" in t
        assert "stop_loss" in t


class TestSellAlertBody:

    def test_body_includes_price_and_pnl_and_reason(self):
        body = format_sell_alert_body(_sell_row())
        assert "BTC" in body
        assert "85" in body
        assert "-15" in body  # loss
        assert "stop-loss" in body.lower()

    def test_body_with_gain_uses_plus_sign(self):
        body = format_sell_alert_body(
            _sell_row(rule="take_profit", price=120.0, pnl=20.0)
        )
        assert "+20" in body

    def test_body_includes_regime_when_set(self):
        body = format_sell_alert_body(_sell_row(regime="risk_off"))
        assert "risk-off" in body.lower() or "adverso" in body.lower()

    def test_body_omits_regime_when_none(self):
        body = format_sell_alert_body(_sell_row(regime=None))
        # No Portuguese regime line should appear.
        assert "risk-off" not in body.lower()
        assert "adverso" not in body.lower()

    def test_body_has_rule_specific_conclusion(self):
        stop = format_sell_alert_body(_sell_row(rule="stop_loss"))
        tp = format_sell_alert_body(
            _sell_row(rule="take_profit", price=120.0, pnl=20.0)
        )
        trail = format_sell_alert_body(
            _sell_row(rule="trailing_stop", price=135.0, pnl=35.0)
        )
        ctx = format_sell_alert_body(
            _sell_row(rule="context_deterioration", price=97.0, pnl=-3.0)
        )
        assert stop != tp != trail != ctx
        assert "proteger capital" in stop
        assert "realizar lucro" in tp
        assert "Travar lucro" in trail or "travar lucro" in trail
        assert "reavaliar" in ctx.lower()

    def test_body_debug_appends_raw_block(self):
        body = format_sell_alert_body(_sell_row(), debug=True)
        assert "--- debug ---" in body
        assert "rule=stop_loss" in body
        assert "severity=high" in body


class TestSellPriorityMap:

    def test_high_maps_to_max_priority(self):
        assert SELL_PRIORITY_BY_SEVERITY["high"] == "max"

    def test_medium_maps_to_high_priority(self):
        assert SELL_PRIORITY_BY_SEVERITY["medium"] == "high"

    def test_unknown_severity_falls_back_to_default(self):
        assert SELL_PRIORITY_BY_SEVERITY.get("xxx", SELL_PRIORITY_DEFAULT) == "default"
