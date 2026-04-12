"""Tests for `crypto_monitor.scheduler.entrypoints`.

These are glue tests: they verify that the three scheduler
entrypoints wire the correct lower-layer calls in the correct order
and surface the results in a `ScanReport` / `MaintenanceReport` /
`WeeklyRunResult`. They deliberately do NOT re-test the business
logic of the modules they orchestrate — each of those has its own
dedicated test file.

What we cover here:
  * scan: seeding, ingestion delegation to an injected client,
    queue flush, signal-processing, isolation of ingest failures
  * weekly: delegation to `generate_and_send_weekly_summary` with
    injected sender
  * maintenance: evaluation + prune + vacuum fan-out, each wrapped
    so one failure does not block the others
  * retention: pure cap behavior (covered here because Block 10
    owns the retention module)

Binance is always stubbed. Ntfy is always stubbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from crypto_monitor.buys import insert_buy
from crypto_monitor.config.settings import (
    BinanceSettings,
    GeneralSettings,
    IntervalsSettings,
    RetentionSettings,
    Settings,
    SymbolsSettings,
)
from crypto_monitor.database.retention import prune_old_candles
from crypto_monitor.notifications.ntfy import (
    REASON_NETWORK_ERROR,
    REASON_SENT,
    SendResult,
)
from crypto_monitor.scheduler import (
    MaintenanceReport,
    ScanReport,
    run_maintenance,
    run_scan,
    run_weekly,
)


UTC = timezone.utc


# ---------- stubs ----------

class _StubBinanceClient:
    """Duck-typed replacement for BinanceClient.

    Captures every `get_klines` call and returns a pre-canned list
    (empty by default). Tests that need the scoring path to fire
    pre-seed candles directly into the DB instead of round-tripping
    through ingestion — that keeps the stub trivial while still
    exercising the scheduler's wiring.
    """

    def __init__(self, klines: list[Any] | None = None) -> None:
        self._klines = klines or []
        self.calls: list[tuple[str, str]] = []

    def get_klines(
        self,
        symbol: str,
        interval: str,
        **kwargs: Any,
    ) -> list[Any]:
        self.calls.append((symbol, interval))
        return list(self._klines)


class _ExplodingBinanceClient:
    """BinanceClient stub that always raises on get_klines."""

    def get_klines(self, *args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("binance boom")


@dataclass
class _SenderCall:
    title: str
    body: str
    priority: str
    tags: tuple[str, ...]


class _RecordingSender:
    """Records every call and returns a pre-canned SendResult."""

    def __init__(self, result: SendResult) -> None:
        self._result = result
        self.calls: list[_SenderCall] = []

    def __call__(
        self,
        ntfy: Any,
        title: str,
        body: str,
        *,
        priority: str = "default",
        tags: tuple[str, ...] = (),
        **_: Any,
    ) -> SendResult:
        self.calls.append(
            _SenderCall(title=title, body=body, priority=priority, tags=tags)
        )
        return self._result


# ---------- settings factory ----------

def _make_settings(
    tmp_path: Path,
    *,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
    tracked: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    auto_seed: bool = True,
    retention_cap: int = 5,
    vacuum_on_maintenance: bool = False,
) -> Settings:
    """Build a full Settings object for scheduler tests.

    The nested scoring / alerts / ntfy / evaluation fixtures from
    conftest are reused verbatim so test behavior tracks production
    defaults. The rest of the fields are minimal plausible values.
    """
    return Settings(
        project_root=tmp_path,
        general=GeneralSettings(
            timezone="America/Sao_Paulo",
            db_path=tmp_path / "unused.db",
            log_dir=tmp_path,
            log_level="INFO",
        ),
        binance=BinanceSettings(
            base_url="https://api.binance.example",
            request_timeout=5,
            retry_count=0,
        ),
        symbols=SymbolsSettings(
            tracked=tracked,
            auto_seed=auto_seed,
        ),
        intervals=IntervalsSettings(
            tracked=("1h", "4h", "1d"),
            bootstrap_limit=250,
        ),
        scoring=scoring_settings,
        alerts=alerts_settings,
        ntfy=ntfy_settings,
        retention=RetentionSettings(
            max_candles_1h=retention_cap,
            max_candles_4h=retention_cap,
            max_candles_1d=retention_cap,
            vacuum_on_maintenance=vacuum_on_maintenance,
        ),
        evaluation=eval_settings,
    )


# ---------- helpers ----------

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_pending_signal(
    conn,
    *,
    symbol: str = "BTCUSDT",
    detected_at: datetime,
    severity: str = "strong",
    score: int = 72,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO signals (
            symbol, detected_at, candle_hour, price_at_signal,
            score, severity, trigger_reason, reversal_signal,
            score_breakdown, alerted
        ) VALUES (?, ?, ?, ?, ?, ?, 'test', 0, '{}', 0)
        """,
        (
            symbol,
            _iso(detected_at),
            _iso(detected_at),
            100.0,
            score,
            severity,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_candle_row(
    conn,
    *,
    symbol: str,
    interval: str,
    open_time: datetime,
    price: float = 100.0,
) -> None:
    iso = _iso(open_time)
    conn.execute(
        """
        INSERT INTO candles
            (symbol, interval, open_time, open, high, low, close, volume, close_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, 100.0, ?)
        """,
        (
            symbol, interval, iso,
            price, price, price, price,
            _iso(open_time + timedelta(hours=1)),
        ),
    )


# ---------- run_scan ----------

def test_run_scan_orchestrates_full_pipeline(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    """Happy-path wiring test.

    Pre-seeds a pending signal so `process_pending_signals` has
    something to dispatch. Uses a stub Binance client that returns
    no klines — ingestion is a no-op, scoring yields no candidates
    (no candles), but the stub is still called, proving the
    scheduler wired ingestion correctly.
    """
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
    )
    client = _StubBinanceClient()
    sender = _RecordingSender(
        SendResult(sent=True, reason=REASON_SENT, status_code=200)
    )

    # Noon in São Paulo (UTC-3) = 15:00 UTC — well outside quiet hours.
    now = datetime(2026, 4, 11, 15, 0, tzinfo=UTC)
    _insert_pending_signal(memory_db, detected_at=now - timedelta(minutes=5))

    report = run_scan(
        settings=settings,
        conn=memory_db,
        client=client,
        now=now,
        sender=sender,
    )

    assert isinstance(report, ScanReport)
    # Seeding populated the symbols table.
    assert report.symbols_seeded == 2
    rows = memory_db.execute(
        "SELECT symbol FROM symbols WHERE active = 1"
    ).fetchall()
    assert {r["symbol"] for r in rows} == {"BTCUSDT", "ETHUSDT"}

    # Ingestion was delegated to the stub client — one call per
    # (symbol, interval) pair.
    assert len(client.calls) == 2 * 3  # 2 symbols × 3 intervals

    # Queue was flushed (nothing queued → considered=0) and alerts
    # processed (1 pending → 1 sent).
    assert report.flush_report is not None
    assert report.flush_report.considered == 0
    assert report.process_report is not None
    assert report.process_report.considered == 1
    assert report.process_report.sent == 1
    assert len(sender.calls) == 1
    assert report.errors == []


def test_run_scan_without_auto_seed_does_not_touch_symbols_table(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
        auto_seed=False,
    )
    client = _StubBinanceClient()

    report = run_scan(
        settings=settings,
        conn=memory_db,
        client=client,
        now=datetime(2026, 4, 11, 15, 0, tzinfo=UTC),
        sender=_RecordingSender(
            SendResult(sent=True, reason=REASON_SENT, status_code=200)
        ),
    )
    assert report.symbols_seeded == 0
    rows = memory_db.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    assert rows == 0
    # No active symbols → client should not be called.
    assert client.calls == []


def test_run_scan_isolates_ingest_failure(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    """An exploding client must not prevent alert processing."""
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
    )
    sender = _RecordingSender(
        SendResult(sent=True, reason=REASON_SENT, status_code=200)
    )
    now = datetime(2026, 4, 11, 15, 0, tzinfo=UTC)
    _insert_pending_signal(memory_db, detected_at=now - timedelta(minutes=5))

    report = run_scan(
        settings=settings,
        conn=memory_db,
        client=_ExplodingBinanceClient(),
        now=now,
        sender=sender,
    )

    # Ingestion failure was captured in the error list — but the
    # alert dispatcher still ran.
    assert any("binance boom" in e or "ingest" in e for e in report.errors)
    assert report.process_report is not None
    assert report.process_report.sent == 1
    assert len(sender.calls) == 1


def test_run_scan_requires_settings_or_project_root(tmp_path):
    with pytest.raises(ValueError, match="project_root.*settings"):
        run_scan()


# ---------- run_weekly ----------

def test_run_weekly_delegates_to_generate_and_send(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
    )
    sender = _RecordingSender(
        SendResult(sent=True, reason=REASON_SENT, status_code=200)
    )
    now = datetime(2026, 4, 11, 15, 0, tzinfo=UTC)

    run = run_weekly(
        settings=settings,
        conn=memory_db,
        now=now,
        sender=sender,
    )

    assert run.summary_id > 0
    assert run.send_result.sent is True
    assert len(sender.calls) == 1
    # Row persisted with sent=1 (end-to-end tested in reports block;
    # here we just verify the entrypoint actually committed it).
    row = memory_db.execute(
        "SELECT sent FROM weekly_summaries WHERE id = ?", (run.summary_id,)
    ).fetchone()
    assert row["sent"] == 1


def test_run_weekly_send_failure_still_persists_row(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
    )
    sender = _RecordingSender(
        SendResult(sent=False, reason=REASON_NETWORK_ERROR, error="boom")
    )
    run = run_weekly(
        settings=settings,
        conn=memory_db,
        now=datetime(2026, 4, 11, 15, 0, tzinfo=UTC),
        sender=sender,
    )

    assert run.send_result.sent is False
    row = memory_db.execute(
        "SELECT sent FROM weekly_summaries WHERE id = ?", (run.summary_id,)
    ).fetchone()
    # Persisted but unsent — a retry can pick it up.
    assert row["sent"] == 0


# ---------- run_maintenance ----------

def test_run_maintenance_evaluates_prunes_and_skips_vacuum_by_default(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    """Full maintenance fan-out on a tiny synthetic DB."""
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
        retention_cap=3,
        vacuum_on_maintenance=False,
    )

    # A matured signal with no future candles → pending verdict but
    # still writes a signal_evaluations row.
    now = datetime(2026, 4, 11, 12, 0, tzinfo=UTC)
    _insert_pending_signal(
        memory_db,
        symbol="BTCUSDT",
        detected_at=now - timedelta(days=35),
    )

    # A matured buy with some same-day candles but no +7d/+30d
    # candles → also pending verdict, row written.
    buy_time = now - timedelta(days=35)
    day_start = buy_time.replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(24):
        _insert_candle_row(
            memory_db,
            symbol="ETHUSDT",
            interval="1h",
            open_time=day_start + timedelta(hours=i),
            price=100.0,
        )
    memory_db.commit()
    insert_buy(
        memory_db,
        symbol="ETHUSDT",
        bought_at=buy_time,
        price=100.0,
        amount_invested=1000.0,
        now=buy_time,
    )

    # Seed 6 1h candles on a third symbol so retention has something
    # to prune (cap=3).
    for i in range(6):
        _insert_candle_row(
            memory_db,
            symbol="SOLUSDT",
            interval="1h",
            open_time=now - timedelta(hours=6 - i),
            price=30.0 + i,
        )
    memory_db.commit()

    report = run_maintenance(
        settings=settings,
        conn=memory_db,
        now=now,
    )

    assert isinstance(report, MaintenanceReport)
    assert report.signal_eval_report is not None
    assert report.signal_eval_report.evaluated == 1
    assert report.buy_eval_report is not None
    assert report.buy_eval_report.evaluated == 1
    assert report.prune_report is not None
    # SOLUSDT had 6 1h candles; cap=3 → 3 pruned.
    assert report.prune_report.total_deleted >= 3
    # Vacuum flag was off.
    assert report.vacuumed is False
    assert report.errors == []


def test_run_maintenance_runs_vacuum_when_flag_is_set(
    memory_db,
    tmp_path,
    scoring_settings,
    alerts_settings,
    ntfy_settings,
    eval_settings,
):
    settings = _make_settings(
        tmp_path,
        scoring_settings=scoring_settings,
        alerts_settings=alerts_settings,
        ntfy_settings=ntfy_settings,
        eval_settings=eval_settings,
        vacuum_on_maintenance=True,
    )
    report = run_maintenance(
        settings=settings,
        conn=memory_db,
        now=datetime(2026, 4, 11, 12, 0, tzinfo=UTC),
    )
    assert report.vacuumed is True
    assert report.errors == []


# ---------- retention unit tests ----------

def test_prune_old_candles_caps_per_symbol_interval(memory_db):
    retention = RetentionSettings(
        max_candles_1h=3,
        max_candles_4h=5,
        max_candles_1d=10,
        vacuum_on_maintenance=False,
    )
    base = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)

    # 7 1h candles for BTCUSDT → expect 3 kept, 4 pruned.
    for i in range(7):
        _insert_candle_row(
            memory_db,
            symbol="BTCUSDT",
            interval="1h",
            open_time=base + timedelta(hours=i),
        )
    # 2 1h candles for ETHUSDT → under cap, nothing pruned.
    for i in range(2):
        _insert_candle_row(
            memory_db,
            symbol="ETHUSDT",
            interval="1h",
            open_time=base + timedelta(hours=i),
        )
    memory_db.commit()

    report = prune_old_candles(memory_db, retention)
    assert report.total_deleted == 4
    assert report.per_interval == {"1h": 4}

    btc_kept = memory_db.execute(
        "SELECT open_time FROM candles WHERE symbol = 'BTCUSDT' AND interval = '1h' "
        "ORDER BY open_time ASC"
    ).fetchall()
    # The NEWEST 3 survived.
    assert len(btc_kept) == 3
    kept_times = [r["open_time"] for r in btc_kept]
    assert kept_times == [
        _iso(base + timedelta(hours=4)),
        _iso(base + timedelta(hours=5)),
        _iso(base + timedelta(hours=6)),
    ]

    eth_kept = memory_db.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol = 'ETHUSDT' AND interval = '1h'"
    ).fetchone()[0]
    assert eth_kept == 2


def test_prune_old_candles_ignores_unknown_intervals(memory_db):
    retention = RetentionSettings(
        max_candles_1h=3,
        max_candles_4h=3,
        max_candles_1d=3,
        vacuum_on_maintenance=False,
    )
    base = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    # '15m' is not in _CAP_BY_INTERVAL → leave untouched.
    for i in range(10):
        _insert_candle_row(
            memory_db,
            symbol="BTCUSDT",
            interval="15m",
            open_time=base + timedelta(minutes=15 * i),
        )
    memory_db.commit()

    report = prune_old_candles(memory_db, retention)
    assert report.total_deleted == 0

    count = memory_db.execute(
        "SELECT COUNT(*) FROM candles WHERE interval = '15m'"
    ).fetchone()[0]
    assert count == 10
