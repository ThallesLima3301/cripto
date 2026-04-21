"""Shared pytest fixtures.

Tests in this project never hit the network and never touch a real
filesystem outside of `:memory:` SQLite.
"""

from __future__ import annotations

import pytest

from crypto_monitor.config.settings import (
    AlertSettings,
    EvaluationSettings,
    NtfySettings,
    ScoringSettings,
    ScoringSeverity,
    ScoringThresholds,
    ScoringWeights,
)
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import run_migrations
from crypto_monitor.database.schema import init_db
from crypto_monitor.indicators import Candle


def _mk(open_time: str, o: float, h: float, l: float, c: float, v: float = 100.0) -> Candle:
    return Candle(
        open_time=open_time,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        close_time=open_time,
    )


@pytest.fixture
def make_candle():
    """Factory for building a Candle with sensible defaults.

    Tests use this when they care about specific OHLC values but don't
    want to repeat the keyword names every time.
    """
    return _mk


@pytest.fixture
def scoring_settings() -> ScoringSettings:
    """A ScoringSettings mirroring `config.example.toml`.

    Hard-coded rather than loaded from the TOML so the signal-engine
    tests don't depend on file parsing, and so tweaking the shipped
    config does not accidentally change test behavior.
    """
    return ScoringSettings(
        weights=ScoringWeights(
            drop_magnitude=25,
            rsi_oversold=20,
            relative_volume=15,
            support_distance=15,
            discount_from_high=10,
            reversal_pattern=10,
            trend_context=5,
        ),
        thresholds=ScoringThresholds(
            min_signal_score=50,
            drop_1h=(1.0, 2.0, 3.0),
            drop_1h_points=(5, 8, 12),
            drop_24h=(3.0, 5.0, 8.0, 12.0),
            drop_24h_points=(8, 12, 18, 22),
            drop_7d=(5.0, 10.0, 15.0, 20.0),
            drop_7d_points=(5, 10, 18, 25),
            drop_30d=(15.0, 25.0, 35.0, 45.0),
            drop_30d_points=(8, 14, 18, 22),
            drop_180d=(30.0, 45.0, 60.0, 75.0),
            drop_180d_points=(6, 10, 14, 18),
            rsi_1h_levels=(30.0, 25.0, 20.0),
            rsi_1h_points=(12, 15, 18),
            rsi_4h_levels=(35.0, 30.0, 25.0),
            rsi_4h_points=(5, 8, 10),
            rel_volume_levels=(1.5, 2.0, 3.0, 4.0),
            rel_volume_points=(5, 9, 12, 15),
            support_distance_levels=(0.5, 1.5, 3.0, 5.0),
            support_distance_points=(15, 12, 8, 4),
            support_lookback_days=90,
            discount_30d_levels=(10.0, 20.0),
            discount_30d_points=(2, 5),
            discount_180d_levels=(20.0, 40.0),
            discount_180d_points=(2, 5),
        ),
        severity=ScoringSeverity(normal=50, strong=65, very_strong=80),
    )


@pytest.fixture
def memory_db():
    """Fresh in-memory SQLite database with the full schema applied.

    Runs both ``init_db`` (baseline tables) and ``run_migrations``
    (incremental deltas) so every test sees the latest schema.
    """
    conn = get_connection(":memory:")
    init_db(conn)
    run_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def alerts_settings() -> AlertSettings:
    """AlertSettings mirroring `config.example.toml`.

    Quiet hours 22..8 local, 2h cooldown, 10-point escalation jump.
    """
    return AlertSettings(
        cooldown_minutes=120,
        escalation_jump=10,
        quiet_hours_start=22,
        quiet_hours_end=8,
    )


@pytest.fixture
def ntfy_settings() -> NtfySettings:
    """NtfySettings with a valid topic so `send_ntfy` won't bail early."""
    return NtfySettings(
        server_url="https://ntfy.example.test",
        topic="test-topic",
        default_tags=("crypto", "v1"),
        request_timeout=5,
        max_retries=2,
        debug_notifications=False,
    )


@pytest.fixture
def eval_settings() -> EvaluationSettings:
    """EvaluationSettings mirroring `config.example.toml`.

    7-day return verdict thresholds:
        >= 10%     great
        >= 5%      good
        -5..5      neutral
        <= -5%     poor
        <= -10%    bad
    """
    return EvaluationSettings(
        great_return_pct=10.0,
        good_return_pct=5.0,
        poor_return_pct=-5.0,
        bad_return_pct=-10.0,
    )


@pytest.fixture
def ntfy_settings_missing_topic() -> NtfySettings:
    """NtfySettings whose topic is empty — validates the missing-topic path."""
    return NtfySettings(
        server_url="https://ntfy.example.test",
        topic="",
        default_tags=("crypto",),
        request_timeout=5,
        max_retries=2,
        debug_notifications=False,
    )
