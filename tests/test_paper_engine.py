"""Economic and timing invariants for the read-only portfolio simulation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import inf, nan

import pytest

from crypto_monitor.paper.engine import simulate
from crypto_monitor.paper.types import HourlyBar, PaperSignal, SimulationConfig


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def hour(value: float) -> datetime:
    return START + timedelta(hours=value)


def signal(
    signal_id: int = 1,
    symbol: str = "ETHUSDT",
    *,
    available: float = 0.25,
    delivered: bool = True,
    score: float = 80.0,
) -> PaperSignal:
    return PaperSignal(
        signal_id=signal_id,
        symbol=symbol,
        candle_hour=hour(-1),
        detected_at=hour(available),
        notified_at=hour(available) if delivered else None,
        score=score,
    )


def bars(symbol: str, prices: list[tuple[float, float]]) -> list[HourlyBar]:
    return [
        HourlyBar(symbol=symbol, open_time=hour(i), open=opening, close=close)
        for i, (opening, close) in enumerate(prices)
    ]


def flat(symbol: str, count: int = 6, price: float = 100.0) -> list[HourlyBar]:
    return bars(symbol, [(price, price)] * count)


def config(**changes: object) -> SimulationConfig:
    return replace(
        SimulationConfig(
            initial_capital=1_000.0,
            position_pct=100.0,
            max_positions=5,
            holding_hours=2,
            fee_bps=0.0,
            slippage_bps=0.0,
            min_score=0.0,
        ),
        **changes,
    )


def run(
    signals: list[PaperSignal] | None = None,
    asset_bars: list[HourlyBar] | None = None,
    *,
    settings: SimulationConfig | None = None,
    end: int = 6,
):
    return simulate(
        [signal()] if signals is None else signals,
        flat("BTCUSDT", end) + (flat("ETHUSDT", end) if asset_bars is None else asset_bars),
        config() if settings is None else settings,
        start=START,
        end=hour(end),
        as_of=hour(end),
    )


def test_flat_market_loses_both_sides_of_fees_and_slippage():
    report = run(settings=config(fee_bps=100, slippage_bps=100))
    # Spend exactly 1,000, including the purchase fee; liquidate at a 1% discount.
    quantity = 1_000 / (101 * 1.01)
    proceeds = quantity * 99 * 0.99
    fees = quantity * 101 * 0.01 + quantity * 99 * 0.01
    trade = report.trades[0]

    assert report.closed_trades == 1
    assert report.open_positions == 0
    assert trade.allocated == pytest.approx(1_000)
    assert trade.quantity == pytest.approx(quantity)
    assert trade.entry_price == pytest.approx(101)
    assert trade.exit_price == pytest.approx(99)
    assert report.ending_cash == pytest.approx(proceeds)
    assert report.ending_equity == pytest.approx(proceeds)
    assert report.net_profit == pytest.approx(proceeds - 1_000)
    assert report.fees_paid == pytest.approx(fees)
    assert report.net_return_pct == pytest.approx((proceeds / 1_000 - 1) * 100)
    assert report.max_drawdown_pct == pytest.approx((1 - proceeds / 1_000) * 100)
    assert trade.net_pnl == pytest.approx(proceeds - 1_000)
    assert report.win_rate_pct == 0
    assert report.benchmark.ending_equity == pytest.approx(proceeds)
    assert report.excess_return_pct == pytest.approx(0)


def test_exit_uses_close_after_exact_holding_period():
    report = run(asset_bars=bars("ETHUSDT", [(100, 1), (100, 700), (110, 120), (900, 900)]), end=4)
    trade = report.trades[0]

    assert datetime.fromisoformat(trade.entry_at) == hour(1)
    assert datetime.fromisoformat(trade.exit_at) == hour(3)
    assert trade.exit_price == 120
    assert report.ending_equity == pytest.approx(1_200)
    assert report.win_rate_pct == 100
    assert report.average_trade_return_pct == pytest.approx(20)


def test_entry_uses_next_hour_open_after_recorded_send_time():
    delayed = replace(signal(), notified_at=hour(3.5))
    prices = [(10, 10), (20, 20), (40, 40), (80, 80), (200, 210), (210, 220)]
    report = run([delayed], bars("ETHUSDT", prices))
    trade = report.trades[0]

    assert datetime.fromisoformat(trade.entry_at) == hour(4)
    assert trade.entry_price == 200
    assert report.ending_equity == pytest.approx(1_100)


def test_receipt_on_hour_boundary_still_waits_until_next_hour():
    report = run([signal(available=1)])
    assert datetime.fromisoformat(report.trades[0].entry_at) == hour(2)


@pytest.mark.parametrize("late_field", ["detected_at", "candle_hour"])
def test_entry_cannot_precede_detection_or_signal_candle_close(late_field):
    candidate = replace(signal(), **{late_field: hour(3 if late_field == "detected_at" else 2)})
    report = run([candidate])
    assert datetime.fromisoformat(report.trades[0].entry_at) == hour(4)


def test_undelivered_alert_is_not_a_trade_in_default_mode():
    report = run([signal(delivered=False)])
    assert report.trades == ()
    assert report.skipped["not_delivered"] == 1
    assert report.ending_equity == 1_000


def test_research_mode_can_include_undelivered_detection():
    report = run([signal(delivered=False)], settings=config(signal_source="detected"))
    assert len(report.trades) == 1
    assert datetime.fromisoformat(report.trades[0].entry_at) == hour(1)


def test_score_filter_is_applied_before_allocating_capital():
    report = run([signal(1, score=49), signal(2, score=50)], settings=config(min_score=50))
    assert [trade.signal_id for trade in report.trades] == [2]


def test_pending_entry_never_uses_a_bar_outside_the_simulation_window():
    report = run([signal(available=5.5)])
    assert report.trades == ()
    assert report.skipped["pending_entry"] == 1
    assert report.ending_cash == 1_000


def test_only_signals_available_inside_requested_window_can_enter():
    old = replace(signal(1, available=-0.5), candle_hour=hour(-2))
    report = run([old, signal(2, available=6), signal(3, available=7)])
    assert report.trades == ()


def test_one_position_per_symbol_prevents_duplicate_exposure():
    report = run(
        [signal(1), signal(2, available=1.25)],
        settings=config(position_pct=40, holding_hours=4),
    )
    assert [trade.signal_id for trade in report.trades] == [1]
    assert report.skipped["position_open"] == 1
    assert report.ending_cash == 1_000


def test_position_limit_caps_simultaneous_different_assets():
    report = run(
        [signal(2, "SOLUSDT"), signal(1)],
        flat("ETHUSDT") + flat("SOLUSDT"),
        settings=config(position_pct=40, max_positions=1),
    )
    assert [trade.signal_id for trade in report.trades] == [1]
    assert report.skipped["position_limit"] == 1


def test_insufficient_cash_does_not_borrow_or_shrink_fixed_position_budget():
    report = run(
        [signal(1), signal(2, "SOLUSDT")],
        flat("ETHUSDT") + flat("SOLUSDT"),
        settings=config(position_pct=60),
    )
    assert [trade.signal_id for trade in report.trades] == [1]
    assert report.trades[0].allocated == 600
    assert report.skipped["insufficient_cash"] == 1
    assert min(point.cash for point in report.equity_curve) == pytest.approx(400)


def test_hour_boundary_exit_frees_cash_and_slot_before_new_entry():
    report = run(
        [signal(1), signal(2, "SOLUSDT", available=1.25)],
        flat("ETHUSDT") + flat("SOLUSDT"),
        settings=config(holding_hours=1, max_positions=1),
    )
    assert [trade.signal_id for trade in report.trades] == [1, 2]
    assert report.closed_trades == 2
    assert report.trades[0].exit_at == report.trades[1].entry_at
    assert report.ending_cash == 1_000


def test_order_is_deterministic_when_alerts_compete_for_the_same_cash():
    candidates = [signal(20, "SOLUSDT"), signal(10, available=0.5), signal(30)]
    market = flat("ETHUSDT") + flat("SOLUSDT")
    first = run(candidates, market)
    second = run(list(reversed(candidates)), list(reversed(market)))

    # Earlier receipt wins first; numeric ID resolves identical timestamps.
    assert [trade.signal_id for trade in first.trades] == [20]
    assert first == second


def test_position_budget_stays_fixed_after_a_profitable_sale():
    market = bars("ETHUSDT", [(100, 100), (100, 200), (200, 200), (200, 200)])
    report = run(
        [signal(1), signal(2, "SOLUSDT", available=1.25)],
        market + flat("SOLUSDT", 4),
        settings=config(holding_hours=1),
        end=4,
    )
    assert [trade.allocated for trade in report.trades] == [1_000, 1_000]
    assert report.ending_equity == 2_000


def test_realized_loss_can_prevent_funding_the_next_fixed_budget():
    market = bars("ETHUSDT", [(100, 100), (100, 50), (50, 50)])
    report = run(
        [signal(1), signal(2, "SOLUSDT", available=1.25)],
        market + flat("SOLUSDT", 3),
        settings=config(holding_hours=1),
        end=3,
    )
    assert [trade.signal_id for trade in report.trades] == [1]
    assert report.skipped["insufficient_cash"] == 1
    assert report.ending_cash == 500


def test_profit_factor_uses_net_winning_and_losing_trades_only():
    market = bars("ETHUSDT", [(100, 100), (100, 120), (120, 120), (120, 120)])
    market += bars("SOLUSDT", [(100, 100), (100, 100), (100, 90), (90, 90)])
    report = run(
        [signal(1), signal(2, "SOLUSDT", available=1.25)],
        market,
        settings=config(holding_hours=1, position_pct=50),
        end=4,
    )
    assert report.closed_trades == 2
    assert report.ending_cash == 1_050
    assert report.win_rate_pct == 50
    assert report.average_trade_return_pct == pytest.approx(5)
    assert report.profit_factor == pytest.approx(2)


def test_non_usdt_quote_is_skipped_without_mixing_units():
    report = run([signal(symbol="ETHBTC")], asset_bars=[])
    assert report.trades == ()
    assert report.skipped["unsupported_quote"] == 1
    assert report.ending_equity == 1_000


def test_unrealized_loss_counts_in_drawdown_even_if_trade_recovers():
    report = run(asset_bars=bars("ETHUSDT", [(100, 100), (100, 50), (50, 100)]), end=3)
    assert report.ending_equity == 1_000
    assert report.net_profit == 0
    assert report.max_drawdown_pct == pytest.approx(50)
    assert min(point.equity for point in report.equity_curve) == 500


def test_opening_gap_counts_in_drawdown_even_when_close_recovers():
    report = run(asset_bars=bars("ETHUSDT", [(100, 100), (100, 100), (25, 100)]), end=3)
    assert report.ending_equity == 1_000
    assert report.max_drawdown_pct == 75
    opening = [point for point in report.equity_curve if point.phase == "open" and datetime.fromisoformat(point.at) == hour(2)]
    assert opening[0].equity == 250


def test_open_position_is_marked_with_estimated_liquidation_costs():
    report = run(
        asset_bars=bars("ETHUSDT", [(100, 100), (100, 125), (125, 150)]),
        settings=config(holding_hours=8, fee_bps=100, slippage_bps=100),
        end=3,
    )
    quantity = 1_000 / (101 * 1.01)
    liquidation = quantity * 150 * 0.99 * 0.99
    assert report.closed_trades == 0
    assert report.open_positions == 1
    assert report.ending_cash == pytest.approx(0)
    assert report.ending_equity == pytest.approx(liquidation)
    assert report.net_profit == pytest.approx(liquidation - 1_000)
    assert report.fees_paid == pytest.approx(quantity * 101 * 0.01)
    assert report.trades[0].exit_at is None
    assert report.trades[0].net_pnl is None
    assert report.win_rate_pct is None


@pytest.mark.parametrize("missing_hour", [1, 2])
def test_missing_entry_or_holding_candle_rejects_incomplete_dataset(missing_hour):
    asset = [bar for bar in flat("ETHUSDT") if bar.open_time != hour(missing_hour)]
    with pytest.raises(ValueError):
        run(asset_bars=asset)


def test_missing_candle_cannot_silently_drop_a_losing_candidate():
    asset = flat("ETHUSDT") + [bar for bar in flat("SOLUSDT") if bar.open_time != hour(1)]
    with pytest.raises(ValueError):
        run([signal(1), signal(2, "SOLUSDT")], asset, settings=config(position_pct=50))


def test_missing_bar_after_trade_has_closed_does_not_invalidate_it():
    report = run(asset_bars=flat("ETHUSDT", 3))
    assert report.closed_trades == 1


def test_missing_prices_for_capacity_skipped_signal_are_not_required():
    report = run(
        [signal(1), signal(2, "SOLUSDT")],
        flat("ETHUSDT"),
        settings=config(max_positions=1),
    )
    assert [trade.signal_id for trade in report.trades] == [1]
    assert report.skipped["position_limit"] == 1


def test_future_prices_cannot_change_the_report():
    market = flat("BTCUSDT", 3) + flat("ETHUSDT", 3)
    kwargs = dict(start=START, end=hour(3), as_of=hour(3.5))
    original = simulate([signal()], market, config(), **kwargs)
    future = [HourlyBar("ETHUSDT", hour(3), 999_999, 999_999)]
    future += [HourlyBar("BTCUSDT", hour(3), 0.0001, 0.0001)]
    assert simulate([signal()], market + future, config(), **kwargs) == original


def test_invalid_future_price_is_ignored_before_validation():
    market = flat("BTCUSDT", 3) + flat("ETHUSDT", 3)
    future = [HourlyBar("ETHUSDT", hour(3), nan, inf)]
    kwargs = dict(start=START, end=hour(3), as_of=hour(3))
    assert simulate([signal()], market + future, config(), **kwargs) == simulate([signal()], market, config(), **kwargs)


def test_benchmark_uses_full_window_and_identical_transaction_costs():
    market = bars("BTCUSDT", [(100, 90), (90, 80), (80, 120)])
    report = simulate(
        [], market, config(fee_bps=100, slippage_bps=100),
        start=START, end=hour(3), as_of=hour(3),
    )
    quantity = 1_000 / (101 * 1.01)
    proceeds = quantity * 120 * 0.99 * 0.99
    assert report.ending_equity == 1_000
    assert report.benchmark.symbol == "BTCUSDT"
    assert report.benchmark.ending_equity == pytest.approx(proceeds)
    assert report.benchmark.net_profit == pytest.approx(proceeds - 1_000)
    assert report.benchmark.net_return_pct == pytest.approx((proceeds / 1_000 - 1) * 100)
    assert report.benchmark.fees_paid == pytest.approx(quantity * 101 * 0.01 + quantity * 120 * 0.99 * 0.01)
    assert report.benchmark.max_drawdown_pct == pytest.approx((1 - quantity * 80 * 0.99 * 0.99 / 1_000) * 100)
    assert report.excess_return_pct == pytest.approx(-report.benchmark.net_return_pct)


def test_benchmark_cannot_skip_missing_hours():
    market = [bar for bar in flat("BTCUSDT") if bar.open_time != hour(3)]
    with pytest.raises(ValueError):
        simulate([], market, config(), start=START, end=hour(6), as_of=hour(6))


@pytest.mark.parametrize("field", ["open", "close"])
@pytest.mark.parametrize("price", [0, -1, nan, inf, -inf])
def test_invalid_prices_reject_dataset(field, price):
    market = flat("ETHUSDT")
    market[1] = replace(market[1], **{field: price})
    with pytest.raises(ValueError):
        run(asset_bars=market)


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_capital": 0}, {"initial_capital": nan}, {"initial_capital": inf},
        {"position_pct": 0}, {"position_pct": 101}, {"position_pct": nan},
        {"max_positions": 0}, {"max_positions": 1.5},
        {"holding_hours": 0}, {"holding_hours": 1.5},
        {"fee_bps": -1}, {"fee_bps": nan}, {"fee_bps": 10_000},
        {"slippage_bps": -1}, {"slippage_bps": inf}, {"slippage_bps": 10_000},
        {"min_score": nan}, {"signal_source": "future"}, {"benchmark_symbol": ""},
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        run(settings=config(**changes))


def test_duplicate_signal_ids_are_rejected():
    with pytest.raises(ValueError):
        run([signal(1), signal(1, "SOLUSDT")], flat("ETHUSDT") + flat("SOLUSDT"))


def test_finite_inputs_cannot_produce_infinite_portfolio_values():
    market = flat("BTCUSDT") + flat("ETHUSDT", price=1e-300)
    with pytest.raises(ValueError, match="finite"):
        simulate(
            [signal()], market, config(initial_capital=1e300),
            start=START, end=hour(6), as_of=hour(6),
        )


def test_duplicate_symbol_hour_bars_are_rejected():
    market = flat("ETHUSDT")
    with pytest.raises(ValueError):
        run(asset_bars=market + [market[1]])


@pytest.mark.parametrize("field", ["detected_at", "candle_hour", "notified_at"])
def test_naive_signal_timestamps_are_rejected(field):
    candidate = replace(signal(), **{field: START.replace(tzinfo=None)})
    with pytest.raises(ValueError):
        run([candidate])


def test_naive_bar_timestamps_are_rejected():
    market = flat("ETHUSDT")
    market[1] = replace(market[1], open_time=hour(1).replace(tzinfo=None))
    with pytest.raises(ValueError):
        run(asset_bars=market)


@pytest.mark.parametrize(
    "start,end,as_of",
    [
        (START.replace(tzinfo=None), hour(6), hour(6)),
        (START, hour(6).replace(tzinfo=None), hour(6)),
        (START, hour(6), hour(6).replace(tzinfo=None)),
        (hour(0.5), hour(6), hour(6)),
        (START, hour(5.5), hour(6)),
        (START, hour(6), hour(5.9)),
        (hour(6), hour(6), hour(6)),
        (hour(7), hour(6), hour(7)),
    ],
)
def test_invalid_or_unfinished_windows_are_rejected(start, end, as_of):
    with pytest.raises(ValueError):
        simulate([], flat("BTCUSDT"), config(), start=start, end=end, as_of=as_of)
