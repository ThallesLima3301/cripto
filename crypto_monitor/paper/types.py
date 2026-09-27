"""Explicit inputs and results for a read-only, hourly portfolio simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SimulationConfig:
    initial_capital: float = 10_000.0
    position_pct: float = 20.0
    max_positions: int = 5
    holding_hours: int = 168
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    min_score: float = 50.0
    benchmark_symbol: str = "BTCUSDT"
    signal_source: str = "delivered"


@dataclass(frozen=True)
class PaperSignal:
    signal_id: int
    symbol: str
    detected_at: datetime
    candle_hour: datetime
    score: float
    # Earliest stored successful send; legacy rows may use batch-start time.
    notified_at: datetime | None = None


@dataclass(frozen=True)
class HourlyBar:
    symbol: str
    open_time: datetime
    open: float
    close: float


@dataclass(frozen=True)
class PaperTrade:
    signal_id: int
    symbol: str
    entry_at: str
    entry_price: float
    quantity: float
    allocated: float
    entry_fee: float
    exit_at: str | None = None
    exit_price: float | None = None
    exit_fee: float | None = None
    net_pnl: float | None = None
    net_return_pct: float | None = None


@dataclass(frozen=True)
class EquityPoint:
    at: str
    phase: str
    equity: float
    cash: float
    positions: int


@dataclass(frozen=True)
class BenchmarkResult:
    symbol: str
    ending_equity: float
    net_profit: float
    net_return_pct: float
    max_drawdown_pct: float
    fees_paid: float


@dataclass(frozen=True)
class SimulationReport:
    config: SimulationConfig
    start: str
    end: str
    signals_considered: int
    skipped: dict[str, int]
    closed_trades: int
    open_positions: int
    ending_cash: float
    ending_equity: float
    net_profit: float
    net_return_pct: float
    max_drawdown_pct: float
    fees_paid: float
    win_rate_pct: float | None
    average_trade_return_pct: float | None
    profit_factor: float | None
    benchmark: BenchmarkResult
    excess_return_pct: float
    trades: tuple[PaperTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    warnings: tuple[str, ...]
