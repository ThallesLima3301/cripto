"""Typed settings loader.

Loads `config.toml` (falling back to `config.example.toml` if the user has
not run `cli init` yet) plus `.env` secrets into a frozen Settings dataclass.
This is the single source of truth for configuration and the ONLY place
TOML/env parsing happens.

All other modules accept a Settings (or one of its nested dataclasses) as a
parameter — nothing reads environment variables or TOML files directly.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# ---------- nested dataclasses (all frozen for safety) ----------

@dataclass(frozen=True)
class GeneralSettings:
    timezone: str
    db_path: Path
    log_dir: Path
    log_level: str


@dataclass(frozen=True)
class BinanceSettings:
    base_url: str
    request_timeout: int
    retry_count: int


@dataclass(frozen=True)
class SymbolsSettings:
    tracked: tuple[str, ...]
    auto_seed: bool


@dataclass(frozen=True)
class IntervalsSettings:
    tracked: tuple[str, ...]
    bootstrap_limit: int


@dataclass(frozen=True)
class ScoringWeights:
    drop_magnitude: int
    rsi_oversold: int
    relative_volume: int
    support_distance: int
    discount_from_high: int
    reversal_pattern: int
    trend_context: int


@dataclass(frozen=True)
class ScoringThresholds:
    min_signal_score: int

    drop_1h: tuple[float, ...]
    drop_1h_points: tuple[int, ...]
    drop_24h: tuple[float, ...]
    drop_24h_points: tuple[int, ...]
    drop_7d: tuple[float, ...]
    drop_7d_points: tuple[int, ...]
    drop_30d: tuple[float, ...]
    drop_30d_points: tuple[int, ...]
    drop_180d: tuple[float, ...]
    drop_180d_points: tuple[int, ...]

    rsi_1h_levels: tuple[float, ...]
    rsi_1h_points: tuple[int, ...]
    rsi_4h_levels: tuple[float, ...]
    rsi_4h_points: tuple[int, ...]

    rel_volume_levels: tuple[float, ...]
    rel_volume_points: tuple[int, ...]

    support_distance_levels: tuple[float, ...]
    support_distance_points: tuple[int, ...]
    support_lookback_days: int

    discount_30d_levels: tuple[float, ...]
    discount_30d_points: tuple[int, ...]
    discount_180d_levels: tuple[float, ...]
    discount_180d_points: tuple[int, ...]


@dataclass(frozen=True)
class ScoringSeverity:
    normal: int
    strong: int
    very_strong: int


@dataclass(frozen=True)
class ScoringSettings:
    weights: ScoringWeights
    thresholds: ScoringThresholds
    severity: ScoringSeverity


@dataclass(frozen=True)
class AlertSettings:
    cooldown_minutes: int
    escalation_jump: int
    quiet_hours_start: int
    quiet_hours_end: int


@dataclass(frozen=True)
class NtfySettings:
    server_url: str
    topic: str
    default_tags: tuple[str, ...]
    request_timeout: int
    max_retries: int
    debug_notifications: bool


@dataclass(frozen=True)
class RetentionSettings:
    max_candles_1h: int
    max_candles_4h: int
    max_candles_1d: int
    vacuum_on_maintenance: bool


@dataclass(frozen=True)
class EvaluationSettings:
    great_return_pct: float
    good_return_pct: float
    poor_return_pct: float
    bad_return_pct: float


@dataclass(frozen=True)
class RegimeSettings:
    """Market regime filter configuration.

    When ``enabled`` is False the entire regime subsystem is skipped and
    no BTC candles are fetched for regime purposes.
    """
    enabled: bool
    ema_short_period: int
    ema_long_period: int
    atr_period: int
    atr_lookback: int
    atr_high_percentile: float
    threshold_adjust_risk_on: int
    threshold_adjust_risk_off: int


@dataclass(frozen=True)
class WatchlistSettings:
    """Watchlist configuration (Block 22, schema + state-machine only).

    The watchlist captures "borderline" scores — below the regular
    emit floor but above ``floor_score`` — so they can be promoted if
    the score climbs past the emit floor within ``max_watch_hours``,
    or quietly expire otherwise. When ``enabled`` is False the
    subsystem is inert regardless of the other fields.
    """
    enabled: bool
    floor_score: int
    max_watch_hours: int


@dataclass(frozen=True)
class SellSettings:
    """Sell-engine configuration (Block 19, schema only).

    The Block 19 surface is data-model only: these fields are parsed and
    surfaced on ``Settings.sell`` so later blocks can flip rules on
    without another config migration. When ``enabled`` is False the
    sell subsystem stays inert regardless of the other thresholds.

    All percentages are in percent (``5.0`` means 5%), positive numbers.
    """
    enabled: bool
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    context_deterioration: bool
    cooldown_hours: int


@dataclass(frozen=True)
class Settings:
    project_root: Path
    general: GeneralSettings
    binance: BinanceSettings
    symbols: SymbolsSettings
    intervals: IntervalsSettings
    scoring: ScoringSettings
    alerts: AlertSettings
    ntfy: NtfySettings
    retention: RetentionSettings
    evaluation: EvaluationSettings
    regime: RegimeSettings
    sell: SellSettings
    watchlist: WatchlistSettings


# ---------- loader ----------

CONFIG_FILENAME = "config.toml"
CONFIG_EXAMPLE_FILENAME = "config.example.toml"
ENV_FILENAME = ".env"


def _resolve_config_path(project_root: Path) -> Path:
    """Return config.toml if present, else fall back to config.example.toml.

    The CLI `init` command is responsible for copying the example to
    config.toml on first run; this fallback keeps tests and first-time
    execution from failing hard before init has been run.
    """
    candidate = project_root / CONFIG_FILENAME
    if candidate.exists():
        return candidate
    example = project_root / CONFIG_EXAMPLE_FILENAME
    if example.exists():
        return example
    raise FileNotFoundError(
        f"Neither {CONFIG_FILENAME} nor {CONFIG_EXAMPLE_FILENAME} "
        f"was found in {project_root}"
    )


def _require(data: dict[str, Any], *path: str) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"Missing config key: {'.'.join(path)}")
        node = node[key]
    return node


def load_settings(project_root: Path) -> Settings:
    """Load and validate all configuration into a frozen Settings object."""
    project_root = Path(project_root).resolve()

    # Load .env (no-op if the file does not exist).
    load_dotenv(project_root / ENV_FILENAME)

    config_path = _resolve_config_path(project_root)
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    g = _require(raw, "general")
    general = GeneralSettings(
        timezone=g["timezone"],
        db_path=(project_root / g["db_path"]).resolve(),
        log_dir=(project_root / g["log_dir"]).resolve(),
        log_level=g.get("log_level", "INFO"),
    )

    b = _require(raw, "binance")
    binance = BinanceSettings(
        base_url=b["base_url"].rstrip("/"),
        request_timeout=int(b["request_timeout"]),
        retry_count=int(b["retry_count"]),
    )

    sym = _require(raw, "symbols")
    symbols = SymbolsSettings(
        tracked=tuple(sym["tracked"]),
        auto_seed=bool(sym.get("auto_seed", True)),
    )

    iv = _require(raw, "intervals")
    intervals = IntervalsSettings(
        tracked=tuple(iv["tracked"]),
        bootstrap_limit=int(iv["bootstrap_limit"]),
    )

    sw = _require(raw, "scoring", "weights")
    weights = ScoringWeights(
        drop_magnitude=int(sw["drop_magnitude"]),
        rsi_oversold=int(sw["rsi_oversold"]),
        relative_volume=int(sw["relative_volume"]),
        support_distance=int(sw["support_distance"]),
        discount_from_high=int(sw["discount_from_high"]),
        reversal_pattern=int(sw["reversal_pattern"]),
        trend_context=int(sw["trend_context"]),
    )
    total = (
        weights.drop_magnitude
        + weights.rsi_oversold
        + weights.relative_volume
        + weights.support_distance
        + weights.discount_from_high
        + weights.reversal_pattern
        + weights.trend_context
    )
    if total != 100:
        raise ValueError(f"Scoring weights must sum to 100 (got {total})")

    st = _require(raw, "scoring", "thresholds")
    thresholds = ScoringThresholds(
        min_signal_score=int(st["min_signal_score"]),
        drop_1h=tuple(float(x) for x in st["drop_1h"]),
        drop_1h_points=tuple(int(x) for x in st["drop_1h_points"]),
        drop_24h=tuple(float(x) for x in st["drop_24h"]),
        drop_24h_points=tuple(int(x) for x in st["drop_24h_points"]),
        drop_7d=tuple(float(x) for x in st["drop_7d"]),
        drop_7d_points=tuple(int(x) for x in st["drop_7d_points"]),
        drop_30d=tuple(float(x) for x in st["drop_30d"]),
        drop_30d_points=tuple(int(x) for x in st["drop_30d_points"]),
        drop_180d=tuple(float(x) for x in st["drop_180d"]),
        drop_180d_points=tuple(int(x) for x in st["drop_180d_points"]),
        rsi_1h_levels=tuple(float(x) for x in st["rsi_1h_levels"]),
        rsi_1h_points=tuple(int(x) for x in st["rsi_1h_points"]),
        rsi_4h_levels=tuple(float(x) for x in st["rsi_4h_levels"]),
        rsi_4h_points=tuple(int(x) for x in st["rsi_4h_points"]),
        rel_volume_levels=tuple(float(x) for x in st["rel_volume_levels"]),
        rel_volume_points=tuple(int(x) for x in st["rel_volume_points"]),
        support_distance_levels=tuple(float(x) for x in st["support_distance_levels"]),
        support_distance_points=tuple(int(x) for x in st["support_distance_points"]),
        support_lookback_days=int(st["support_lookback_days"]),
        discount_30d_levels=tuple(float(x) for x in st["discount_30d_levels"]),
        discount_30d_points=tuple(int(x) for x in st["discount_30d_points"]),
        discount_180d_levels=tuple(float(x) for x in st["discount_180d_levels"]),
        discount_180d_points=tuple(int(x) for x in st["discount_180d_points"]),
    )

    sv = _require(raw, "scoring", "severity")
    severity = ScoringSeverity(
        normal=int(sv["normal"]),
        strong=int(sv["strong"]),
        very_strong=int(sv["very_strong"]),
    )

    scoring = ScoringSettings(
        weights=weights,
        thresholds=thresholds,
        severity=severity,
    )

    al = _require(raw, "alerts")
    alerts = AlertSettings(
        cooldown_minutes=int(al["cooldown_minutes"]),
        escalation_jump=int(al["escalation_jump"]),
        quiet_hours_start=int(al["quiet_hours_start"]),
        quiet_hours_end=int(al["quiet_hours_end"]),
    )

    nt = _require(raw, "ntfy")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    ntfy = NtfySettings(
        server_url=nt["server_url"].rstrip("/"),
        topic=topic,
        default_tags=tuple(nt.get("default_tags", [])),
        request_timeout=int(nt["request_timeout"]),
        max_retries=int(nt["max_retries"]),
        debug_notifications=bool(nt.get("debug_notifications", False)),
    )

    rt = _require(raw, "retention")
    retention = RetentionSettings(
        max_candles_1h=int(rt["max_candles_1h"]),
        max_candles_4h=int(rt["max_candles_4h"]),
        max_candles_1d=int(rt["max_candles_1d"]),
        vacuum_on_maintenance=bool(rt.get("vacuum_on_maintenance", False)),
    )

    ev = _require(raw, "evaluation")
    evaluation = EvaluationSettings(
        great_return_pct=float(ev["great_return_pct"]),
        good_return_pct=float(ev["good_return_pct"]),
        poor_return_pct=float(ev["poor_return_pct"]),
        bad_return_pct=float(ev["bad_return_pct"]),
    )

    # Optional v2 sections — absent sections default to feature-disabled.
    rg = raw.get("regime", {})
    regime = RegimeSettings(
        enabled=bool(rg.get("enabled", False)),
        ema_short_period=int(rg.get("ema_short_period", 20)),
        ema_long_period=int(rg.get("ema_long_period", 50)),
        atr_period=int(rg.get("atr_period", 14)),
        atr_lookback=int(rg.get("atr_lookback", 90)),
        atr_high_percentile=float(rg.get("atr_high_percentile", 70.0)),
        threshold_adjust_risk_on=int(rg.get("threshold_adjust_risk_on", -5)),
        threshold_adjust_risk_off=int(rg.get("threshold_adjust_risk_off", 5)),
    )

    sl = raw.get("sell", {})
    sell = SellSettings(
        enabled=bool(sl.get("enabled", False)),
        stop_loss_pct=float(sl.get("stop_loss_pct", 8.0)),
        take_profit_pct=float(sl.get("take_profit_pct", 20.0)),
        trailing_stop_pct=float(sl.get("trailing_stop_pct", 10.0)),
        context_deterioration=bool(sl.get("context_deterioration", True)),
        cooldown_hours=int(sl.get("cooldown_hours", 6)),
    )

    wl = raw.get("watchlist", {})
    watchlist = WatchlistSettings(
        enabled=bool(wl.get("enabled", False)),
        floor_score=int(wl.get("floor_score", 35)),
        max_watch_hours=int(wl.get("max_watch_hours", 48)),
    )

    return Settings(
        project_root=project_root,
        general=general,
        binance=binance,
        symbols=symbols,
        intervals=intervals,
        scoring=scoring,
        alerts=alerts,
        ntfy=ntfy,
        retention=retention,
        evaluation=evaluation,
        regime=regime,
        sell=sell,
        watchlist=watchlist,
    )
