"""Deterministic hourly replay with finite cash and observable execution times.

The model uses recorded signals, not a retrospective rescore. It does not
send orders, infer intrabar stop fills, or drop losing trades when data is
missing. Missing prices required by an open position invalidate the run.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Sequence

from crypto_monitor.paper.types import (
    BenchmarkResult, EquityPoint, HourlyBar, PaperSignal, PaperTrade,
    SimulationConfig, SimulationReport,
)
from crypto_monitor.utils.time_utils import floor_to_hour, to_utc_iso


HOUR = timedelta(hours=1)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: float, name: str) -> None:
    try:
        valid = not isinstance(value, bool) and math.isfinite(value)
    except TypeError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be a finite number")


def validate_config(config: SimulationConfig) -> None:
    """Reject invalid assumptions instead of manufacturing an equity curve."""
    for name in ("initial_capital", "position_pct", "fee_bps", "slippage_bps", "min_score"):
        _finite(getattr(config, name), name)
    if config.initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not 0 < config.position_pct <= 100:
        raise ValueError("position_pct must be in (0, 100]")
    for name in ("max_positions", "holding_hours"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if not 0 <= config.fee_bps < 10_000 or not 0 <= config.slippage_bps < 10_000:
        raise ValueError("fee_bps and slippage_bps must be in [0, 10000)")
    if not 0 <= config.min_score <= 100:
        raise ValueError("min_score must be in [0, 100]")
    if config.signal_source not in ("delivered", "detected"):
        raise ValueError("signal_source must be delivered or detected")
    if not config.benchmark_symbol.endswith("USDT") or config.benchmark_symbol == "USDT":
        raise ValueError("benchmark_symbol must be a USDT pair")


@dataclass
class _Position:
    trade_index: int
    due: datetime


def simulate(
    signals: Sequence[PaperSignal],
    bars: Sequence[HourlyBar],
    config: SimulationConfig,
    *,
    start: datetime,
    end: datetime,
    as_of: datetime,
) -> SimulationReport:
    """Replay [start, end) using only candles closed by ``as_of``.

    Entry is the strictly next hourly open after signal availability.
    Exit is the close exactly ``holding_hours`` after entry. At the end,
    unfinished positions remain open and are valued after estimated exit
    costs. Drawdown samples both opens and closes, not intrahour extrema.
    """
    validate_config(config)
    start, end, as_of = (_utc(start, "start"), _utc(end, "end"), _utc(as_of, "as_of"))
    if start != floor_to_hour(start) or end != floor_to_hour(end):
        raise ValueError("start and end must be aligned to UTC hours")
    if start >= end:
        raise ValueError("start must be before end")
    if end > floor_to_hour(as_of):
        raise ValueError("end must not include candles that are not yet closed")

    prices: dict[tuple[str, datetime], HourlyBar] = {}
    for bar in bars:
        opening = _utc(bar.open_time, "bar.open_time")
        if not start <= opening < end:
            continue
        if opening != floor_to_hour(opening):
            raise ValueError("bar.open_time must be aligned to UTC hours")
        key = (bar.symbol, opening)
        if key in prices:
            raise ValueError(f"duplicate candle for {bar.symbol} at {to_utc_iso(opening)}")
        for name in ("open", "close"):
            value = getattr(bar, name)
            _finite(value, f"bar.{name}")
            if value <= 0:
                raise ValueError(f"bar.{name} must be positive")
        prices[key] = bar

    def price(symbol: str, at: datetime) -> HourlyBar:
        bar = prices.get((symbol, at))
        if bar is None:
            raise ValueError(
                f"missing 1h candle for {symbol} at {to_utc_iso(at)}; simulation incomplete"
            )
        return bar

    skipped: Counter[str] = Counter()
    events: dict[datetime, list[tuple[datetime, PaperSignal]]] = defaultdict(list)
    considered = 0
    seen_ids: set[int] = set()
    for signal in signals:
        if signal.signal_id in seen_ids:
            raise ValueError(f"duplicate signal id: {signal.signal_id}")
        seen_ids.add(signal.signal_id)
        available = max(
            _utc(signal.candle_hour, "signal.candle_hour") + HOUR,
            _utc(signal.detected_at, "signal.detected_at"),
        )
        if config.signal_source == "delivered":
            if signal.notified_at is None:
                if start <= available < end:
                    considered += 1
                    skipped["not_delivered"] += 1
                continue
            available = max(available, _utc(signal.notified_at, "signal.notified_at"))
        if not start <= available < end:
            continue
        considered += 1
        _finite(signal.score, "signal.score")
        if signal.score < config.min_score:
            skipped["below_score"] += 1
            continue
        if not signal.symbol.endswith("USDT") or signal.symbol == "USDT":
            skipped["unsupported_quote"] += 1
            continue
        entry = floor_to_hour(available) + HOUR
        if entry >= end:
            skipped["pending_entry"] += 1
            continue
        events[entry].append((available, signal))

    fee = config.fee_bps / 10_000
    slip = config.slippage_bps / 10_000
    sell_factor = (1 - slip) * (1 - fee)
    budget = config.initial_capital * (config.position_pct / 100)
    if budget <= 0:
        raise ValueError("position budget is too small for numeric precision")
    cash = config.initial_capital
    tolerance = config.initial_capital * 1e-12
    trades: list[PaperTrade] = []
    positions: dict[str, _Position] = {}
    curve = [EquityPoint(to_utc_iso(start), "initial", cash, cash, 0)]
    peak = cash
    drawdown = 0.0
    fees_paid = 0.0

    # Benchmark assets are fixed in the config, never chosen from future
    # winners or the universe of signals that happened to fire later.
    benchmark_entry = price(config.benchmark_symbol, start).open * (1 + slip)
    _finite(benchmark_entry, "benchmark entry price")
    benchmark_quantity = config.initial_capital / (benchmark_entry * (1 + fee))
    _finite(benchmark_quantity, "benchmark quantity")
    if benchmark_quantity <= 0:
        raise ValueError("benchmark quantity is too small for numeric precision")
    benchmark_fees = benchmark_quantity * benchmark_entry * fee
    benchmark_peak = config.initial_capital
    benchmark_drawdown = 0.0
    benchmark_equity = config.initial_capital

    def mark(at: datetime, opening: datetime, phase: str) -> None:
        nonlocal peak, drawdown, benchmark_peak, benchmark_drawdown, benchmark_equity
        field = "open" if phase == "open" else "close"
        values = (
            trades[p.trade_index].quantity * getattr(price(symbol, opening), field) * sell_factor
            for symbol, p in positions.items()
        )
        equity = cash + math.fsum(values)
        _finite(equity, "portfolio equity")
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)
        curve.append(EquityPoint(to_utc_iso(at), phase, equity, cash, len(positions)))
        benchmark_equity = (
            benchmark_quantity * getattr(price(config.benchmark_symbol, opening), field) * sell_factor
        )
        _finite(benchmark_equity, "benchmark equity")
        benchmark_peak = max(benchmark_peak, benchmark_equity)
        benchmark_drawdown = max(
            benchmark_drawdown, (benchmark_peak - benchmark_equity) / benchmark_peak * 100,
        )

    current = start
    while current < end:
        for _, signal in sorted(events.get(current, []), key=lambda item: (item[0], item[1].signal_id)):
            if signal.symbol in positions:
                skipped["position_open"] += 1
                continue
            if len(positions) >= config.max_positions:
                skipped["position_limit"] += 1
                continue
            if cash + tolerance < budget:
                skipped["insufficient_cash"] += 1
                continue
            execution = price(signal.symbol, current).open * (1 + slip)
            _finite(execution, "entry price")
            quantity = budget / (execution * (1 + fee))
            _finite(quantity, "position quantity")
            if quantity <= 0:
                raise ValueError("position quantity is too small for numeric precision")
            entry_fee = quantity * execution * fee
            trades.append(PaperTrade(
                signal_id=signal.signal_id, symbol=signal.symbol,
                entry_at=to_utc_iso(current), entry_price=execution,
                quantity=quantity, allocated=budget, entry_fee=entry_fee,
            ))
            positions[signal.symbol] = _Position(
                len(trades) - 1, current + timedelta(hours=config.holding_hours),
            )
            cash = max(0.0, cash - budget)
            fees_paid += entry_fee

        mark(current, current, "open")
        close_at = current + HOUR
        for symbol, position in list(positions.items()):
            if position.due != close_at:
                continue
            trade = trades[position.trade_index]
            execution = price(symbol, current).close * (1 - slip)
            exit_fee = trade.quantity * execution * fee
            proceeds = trade.quantity * execution - exit_fee
            net = proceeds - trade.allocated
            cash += proceeds
            fees_paid += exit_fee
            trades[position.trade_index] = replace(
                trade, exit_at=to_utc_iso(close_at), exit_price=execution,
                exit_fee=exit_fee, net_pnl=net, net_return_pct=net / trade.allocated * 100,
            )
            del positions[symbol]
        mark(close_at, current, "close")
        current = close_at

    benchmark_exit = price(config.benchmark_symbol, end - HOUR).close * (1 - slip)
    benchmark_fees += benchmark_quantity * benchmark_exit * fee
    benchmark_return = (benchmark_equity / config.initial_capital - 1) * 100
    benchmark = BenchmarkResult(
        config.benchmark_symbol, benchmark_equity,
        benchmark_equity - config.initial_capital, benchmark_return,
        benchmark_drawdown, benchmark_fees,
    )
    closed = [trade for trade in trades if trade.exit_at is not None]
    wins = [trade.net_pnl for trade in closed if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in closed if trade.net_pnl < 0]
    equity = curve[-1].equity
    net_return = (equity / config.initial_capital - 1) * 100
    _finite(net_return, "net return")
    _finite(benchmark_return, "benchmark return")
    warnings = [
        "Simulação de sinais registrados; não recria sinais anteriores nem comprova lucro futuro.",
        "Taxas e slippage são hipóteses por lado; não incluem impostos nem impacto por tamanho da ordem.",
        "Drawdown medido nas aberturas e fechamentos horários; perdas intrahora podem ser maiores.",
    ]
    if len(closed) < 30:
        warnings.append("Menos de 30 operações encerradas: amostra pequena para avaliar a estratégia.")
    if positions:
        warnings.append(
            "Posições abertas incluídas pelo valor líquido estimado de liquidação; lucro ainda não realizado."
        )
    if config.signal_source == "detected":
        warnings.append("Modo detected usa a detecção do sinal, independentemente da entrega da notificação.")
    else:
        warnings.append(
            "Envios antigos podem ter horário do início do scan/lote; nesses registros a entrada pode estar antecipada. "
            "A confirmação do servidor também não comprova recebimento no celular."
        )

    return SimulationReport(
        config=config, start=to_utc_iso(start), end=to_utc_iso(end),
        signals_considered=considered, skipped=dict(sorted(skipped.items())),
        closed_trades=len(closed), open_positions=len(positions),
        ending_cash=cash, ending_equity=equity,
        net_profit=equity - config.initial_capital, net_return_pct=net_return,
        max_drawdown_pct=drawdown, fees_paid=fees_paid,
        win_rate_pct=len(wins) / len(closed) * 100 if closed else None,
        average_trade_return_pct=(
            math.fsum(trade.net_return_pct for trade in closed) / len(closed) if closed else None
        ),
        profit_factor=math.fsum(wins) / abs(math.fsum(losses)) if losses else None,
        benchmark=benchmark, excess_return_pct=net_return - benchmark_return,
        trades=tuple(trades), equity_curve=tuple(curve), warnings=tuple(warnings),
    )
