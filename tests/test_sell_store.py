"""Block 19 — sell-engine data model and persistence.

Tests pin the schema produced by migration 003 plus the small helpers
in ``crypto_monitor.sell.store``. No business logic — every test
exercises shape and validation, not "should we sell?".

Coverage:
  * migration 003 creates the new tables, indexes, and columns.
  * ``SellSettings`` parses with safe defaults when ``[sell]`` is absent.
  * ``upsert_high_watermark`` / ``get_high_watermark``: insert + monotone update.
  * ``insert_sell_signal`` / ``last_sell_signal_time``: round-trip + lookup.
  * ``load_open_buys`` excludes already-sold rows and respects ``symbol``.
  * ``record_sale``: happy path + the three validation rules
    (no double-sell, positive sold_price, sold_at >= bought_at).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crypto_monitor.buys.manual import insert_buy
from crypto_monitor.config.settings import load_settings
from crypto_monitor.database.connection import get_connection
from crypto_monitor.database.migrations import (
    column_exists,
    run_migrations,
    table_exists,
)
from crypto_monitor.database.schema import init_db
from crypto_monitor.sell import (
    SellSignal,
    get_high_watermark,
    insert_sell_signal,
    last_sell_signal_time,
    load_open_buys,
    record_sale,
    upsert_high_watermark,
)


UTC = timezone.utc


# ---------- fixtures ----------

@pytest.fixture
def db():
    """In-memory DB with the full schema (init + all real migrations)."""
    conn = get_connection(":memory:")
    init_db(conn)
    run_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _make_buy(conn: sqlite3.Connection, *, symbol: str = "BTCUSDT", price: float = 100.0,
              when: datetime | None = None) -> int:
    """Insert a minimal buy row and return its id."""
    when = when or datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    rec = insert_buy(
        conn,
        symbol=symbol,
        bought_at=when,
        price=price,
        amount_invested=1000.0,
        now=when,
    )
    return rec.id


# ---------- migration 003 ----------

class TestMigration003Schema:

    def test_sell_tracking_table_created(self, db):
        assert table_exists(db, "sell_tracking")

    def test_sell_signals_table_created(self, db):
        assert table_exists(db, "sell_signals")

    def test_sell_tracking_has_composite_pk(self, db):
        # Two distinct (symbol, buy_id) pairs must coexist; a duplicate
        # pair must collide on the PRIMARY KEY.
        buy_a = _make_buy(db, symbol="BTCUSDT")
        buy_b = _make_buy(db, symbol="ETHUSDT")
        when = datetime(2026, 4, 1, 13, 0, tzinfo=UTC)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_a,
                              high_watermark=110.0, now=when)
        upsert_high_watermark(db, symbol="ETHUSDT", buy_id=buy_b,
                              high_watermark=210.0, now=when)
        rows = db.execute(
            "SELECT symbol, buy_id, high_watermark FROM sell_tracking "
            "ORDER BY symbol"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["symbol"] == "BTCUSDT"
        assert rows[1]["symbol"] == "ETHUSDT"

    def test_buys_has_sold_columns(self, db):
        for col in ("sold_at", "sold_price", "sold_note"):
            assert column_exists(db, "buys", col), f"missing column: {col}"

    def test_sell_signals_indexes_created(self, db):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='sell_signals'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_sell_signals_symbol_time" in names
        assert "idx_sell_signals_buy" in names

    def test_migration_is_idempotent(self, db):
        # Running again must not raise even though every guard already passed.
        report = run_migrations(db)
        assert report.steps_applied == ()
        assert table_exists(db, "sell_tracking")
        assert table_exists(db, "sell_signals")


# ---------- SellSettings loader ----------

class TestSellSettingsLoader:

    def _write_config(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def _minimal_required(self) -> str:
        # Smallest TOML that still satisfies every required section.
        # Mirrors the live config.example.toml shape.
        return """
[general]
timezone = "UTC"
db_path = "data/x.db"
log_dir = "logs"
log_level = "INFO"

[binance]
base_url = "https://example.test"
request_timeout = 5
retry_count = 1

[symbols]
tracked = ["BTCUSDT"]
auto_seed = true

[intervals]
tracked = ["1h"]
bootstrap_limit = 100

[scoring.weights]
drop_magnitude = 25
rsi_oversold = 20
relative_volume = 15
support_distance = 15
discount_from_high = 10
reversal_pattern = 10
trend_context = 5

[scoring.thresholds]
min_signal_score = 50
drop_1h          = [1.0]
drop_1h_points   = [5]
drop_24h         = [3.0]
drop_24h_points  = [8]
drop_7d          = [5.0]
drop_7d_points   = [5]
drop_30d         = [15.0]
drop_30d_points  = [8]
drop_180d        = [30.0]
drop_180d_points = [6]
rsi_1h_levels = [30]
rsi_1h_points = [12]
rsi_4h_levels = [35]
rsi_4h_points = [5]
rel_volume_levels = [1.5]
rel_volume_points = [5]
support_distance_levels = [0.5]
support_distance_points = [15]
support_lookback_days = 90
discount_30d_levels = [10.0]
discount_30d_points = [2]
discount_180d_levels = [20.0]
discount_180d_points = [2]

[scoring.severity]
normal = 50
strong = 65
very_strong = 80

[alerts]
cooldown_minutes = 120
escalation_jump = 10
quiet_hours_start = 22
quiet_hours_end   = 8

[ntfy]
server_url = "https://ntfy.test"
default_tags = ["x"]
request_timeout = 5
max_retries = 1

[retention]
max_candles_1h = 100
max_candles_4h = 100
max_candles_1d = 100

[evaluation]
great_return_pct = 10.0
good_return_pct  = 5.0
poor_return_pct  = -5.0
bad_return_pct   = -10.0
"""

    def test_missing_sell_section_uses_defaults(self, tmp_path):
        self._write_config(tmp_path, self._minimal_required())
        settings = load_settings(tmp_path)
        assert settings.sell.enabled is False
        assert settings.sell.stop_loss_pct == pytest.approx(8.0)
        assert settings.sell.take_profit_pct == pytest.approx(20.0)
        assert settings.sell.trailing_stop_pct == pytest.approx(10.0)
        assert settings.sell.context_deterioration is True
        assert settings.sell.cooldown_hours == 6

    def test_explicit_sell_section_is_parsed(self, tmp_path):
        body = self._minimal_required() + """
[sell]
enabled = true
stop_loss_pct = 5.5
take_profit_pct = 12.0
trailing_stop_pct = 7.5
context_deterioration = false
cooldown_hours = 3
"""
        self._write_config(tmp_path, body)
        settings = load_settings(tmp_path)
        assert settings.sell.enabled is True
        assert settings.sell.stop_loss_pct == pytest.approx(5.5)
        assert settings.sell.take_profit_pct == pytest.approx(12.0)
        assert settings.sell.trailing_stop_pct == pytest.approx(7.5)
        assert settings.sell.context_deterioration is False
        assert settings.sell.cooldown_hours == 3

    def test_example_config_parses(self):
        """The shipped config.example.toml must include a valid [sell] block."""
        settings = load_settings(Path("."))
        assert settings.sell.enabled is False
        assert settings.sell.cooldown_hours == 6


# ---------- high-water-mark store ----------

class TestHighWatermark:

    def test_get_returns_none_for_unknown(self, db):
        buy_id = _make_buy(db)
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) is None

    def test_insert_then_read(self, db):
        buy_id = _make_buy(db)
        upsert_high_watermark(
            db, symbol="BTCUSDT", buy_id=buy_id,
            high_watermark=120.5,
            now=datetime(2026, 4, 1, 13, 0, tzinfo=UTC),
        )
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 120.5

    def test_upsert_keeps_higher_value(self, db):
        buy_id = _make_buy(db)
        when = datetime(2026, 4, 1, 13, 0, tzinfo=UTC)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id,
                              high_watermark=120.0, now=when)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id,
                              high_watermark=150.0, now=when)
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 150.0

    def test_upsert_does_not_lower_value(self, db):
        buy_id = _make_buy(db)
        when = datetime(2026, 4, 1, 13, 0, tzinfo=UTC)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id,
                              high_watermark=150.0, now=when)
        # A subsequent lower observation must NOT lower the watermark.
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id,
                              high_watermark=140.0, now=when)
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_id) == 150.0

    def test_per_position_isolation(self, db):
        """Two open buys on the same symbol keep independent watermarks."""
        buy_a = _make_buy(db, symbol="BTCUSDT",
                          when=datetime(2026, 4, 1, 12, 0, tzinfo=UTC))
        buy_b = _make_buy(db, symbol="BTCUSDT",
                          when=datetime(2026, 4, 2, 12, 0, tzinfo=UTC))
        when = datetime(2026, 4, 3, 0, 0, tzinfo=UTC)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_a,
                              high_watermark=110.0, now=when)
        upsert_high_watermark(db, symbol="BTCUSDT", buy_id=buy_b,
                              high_watermark=130.0, now=when)
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_a) == 110.0
        assert get_high_watermark(db, symbol="BTCUSDT", buy_id=buy_b) == 130.0

    def test_rejects_non_positive_watermark(self, db):
        buy_id = _make_buy(db)
        with pytest.raises(ValueError, match="high_watermark"):
            upsert_high_watermark(
                db, symbol="BTCUSDT", buy_id=buy_id, high_watermark=0.0,
            )


# ---------- sell-signals log ----------

class TestSellSignalsLog:

    def _signal(self, *, buy_id: int, when: datetime,
                price: float = 95.0, rule: str = "stop_loss") -> SellSignal:
        return SellSignal(
            id=None,
            symbol="BTCUSDT",
            buy_id=buy_id,
            detected_at=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            price_at_signal=price,
            rule_triggered=rule,
            severity="warn",
            reason=f"{rule} fired at {price}",
            pnl_pct=-5.0,
            regime_at_signal="risk_off",
        )

    def test_insert_returns_id_and_persists_columns(self, db):
        buy_id = _make_buy(db)
        when = datetime(2026, 4, 2, 10, 0, tzinfo=UTC)
        new_id = insert_sell_signal(db, self._signal(buy_id=buy_id, when=when))
        assert new_id > 0

        row = db.execute(
            "SELECT * FROM sell_signals WHERE id = ?", (new_id,)
        ).fetchone()
        assert row["symbol"] == "BTCUSDT"
        assert row["buy_id"] == buy_id
        assert row["detected_at"] == "2026-04-02T10:00:00Z"
        assert row["price_at_signal"] == 95.0
        assert row["rule_triggered"] == "stop_loss"
        assert row["severity"] == "warn"
        assert row["pnl_pct"] == -5.0
        assert row["regime_at_signal"] == "risk_off"
        assert row["alerted"] == 0

    def test_insert_rejects_non_positive_price(self, db):
        buy_id = _make_buy(db)
        when = datetime(2026, 4, 2, 10, 0, tzinfo=UTC)
        bad = self._signal(buy_id=buy_id, when=when, price=0.0)
        with pytest.raises(ValueError, match="price_at_signal"):
            insert_sell_signal(db, bad)

    def test_insert_rejects_empty_rule(self, db):
        buy_id = _make_buy(db)
        when = datetime(2026, 4, 2, 10, 0, tzinfo=UTC)
        bad = self._signal(buy_id=buy_id, when=when, rule="")
        with pytest.raises(ValueError, match="rule_triggered"):
            insert_sell_signal(db, bad)

    def test_last_sell_signal_time_returns_none_when_empty(self, db):
        buy_id = _make_buy(db)
        assert last_sell_signal_time(db, buy_id=buy_id) is None

    def test_last_sell_signal_time_returns_most_recent(self, db):
        buy_id = _make_buy(db)
        early = datetime(2026, 4, 2, 10, 0, tzinfo=UTC)
        later = datetime(2026, 4, 2, 14, 30, tzinfo=UTC)
        insert_sell_signal(db, self._signal(buy_id=buy_id, when=early))
        insert_sell_signal(db, self._signal(buy_id=buy_id, when=later,
                                             rule="trailing_stop"))
        assert last_sell_signal_time(db, buy_id=buy_id) == "2026-04-02T14:30:00Z"

    def test_last_sell_signal_time_per_buy(self, db):
        """Two buys' logs do not bleed into each other."""
        buy_a = _make_buy(db, symbol="BTCUSDT",
                          when=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        buy_b = _make_buy(db, symbol="ETHUSDT",
                          when=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        only_a = datetime(2026, 4, 2, 9, 0, tzinfo=UTC)
        insert_sell_signal(db, self._signal(buy_id=buy_a, when=only_a))
        assert last_sell_signal_time(db, buy_id=buy_a) == "2026-04-02T09:00:00Z"
        assert last_sell_signal_time(db, buy_id=buy_b) is None


# ---------- load_open_buys ----------

class TestLoadOpenBuys:

    def test_excludes_sold_rows(self, db):
        open_id = _make_buy(db, symbol="BTCUSDT",
                            when=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        sold_id = _make_buy(db, symbol="ETHUSDT",
                            when=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        record_sale(
            db,
            buy_id=sold_id,
            sold_at=datetime(2026, 4, 5, 10, 0, tzinfo=UTC),
            sold_price=110.0,
        )

        ids = [b.id for b in load_open_buys(db)]
        assert open_id in ids
        assert sold_id not in ids

    def test_filters_by_symbol(self, db):
        btc_id = _make_buy(db, symbol="BTCUSDT")
        _eth_id = _make_buy(db, symbol="ETHUSDT")
        rows = load_open_buys(db, symbol="BTCUSDT")
        assert [b.id for b in rows] == [btc_id]

    def test_orders_oldest_first(self, db):
        first_id = _make_buy(db, symbol="BTCUSDT",
                             when=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        second_id = _make_buy(db, symbol="BTCUSDT",
                              when=datetime(2026, 4, 2, 0, 0, tzinfo=UTC))
        rows = load_open_buys(db, symbol="BTCUSDT")
        assert [b.id for b in rows] == [first_id, second_id]


# ---------- record_sale validation ----------

class TestRecordSale:

    def _bought(self) -> datetime:
        return datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    def test_happy_path_marks_buy_sold(self, db):
        buy_id = _make_buy(db, when=self._bought())
        record_sale(
            db,
            buy_id=buy_id,
            sold_at=self._bought() + timedelta(days=3),
            sold_price=120.0,
            sold_note="took profit",
        )
        row = db.execute(
            "SELECT sold_at, sold_price, sold_note FROM buys WHERE id = ?",
            (buy_id,),
        ).fetchone()
        assert row["sold_at"] == "2026-04-04T12:00:00Z"
        assert row["sold_price"] == 120.0
        assert row["sold_note"] == "took profit"

    def test_rejects_double_sell(self, db):
        buy_id = _make_buy(db, when=self._bought())
        record_sale(
            db,
            buy_id=buy_id,
            sold_at=self._bought() + timedelta(days=1),
            sold_price=110.0,
        )
        with pytest.raises(ValueError, match="already marked sold"):
            record_sale(
                db,
                buy_id=buy_id,
                sold_at=self._bought() + timedelta(days=2),
                sold_price=115.0,
            )

    def test_rejects_non_positive_sold_price(self, db):
        buy_id = _make_buy(db, when=self._bought())
        with pytest.raises(ValueError, match="sold_price"):
            record_sale(
                db,
                buy_id=buy_id,
                sold_at=self._bought() + timedelta(days=1),
                sold_price=0.0,
            )
        with pytest.raises(ValueError, match="sold_price"):
            record_sale(
                db,
                buy_id=buy_id,
                sold_at=self._bought() + timedelta(days=1),
                sold_price=-5.0,
            )

    def test_rejects_sold_at_before_bought_at(self, db):
        buy_id = _make_buy(db, when=self._bought())
        with pytest.raises(ValueError, match="earlier than bought_at"):
            record_sale(
                db,
                buy_id=buy_id,
                sold_at=self._bought() - timedelta(hours=1),
                sold_price=120.0,
            )

    def test_rejects_naive_sold_at(self, db):
        buy_id = _make_buy(db, when=self._bought())
        with pytest.raises(ValueError, match="timezone-aware"):
            record_sale(
                db,
                buy_id=buy_id,
                sold_at=datetime(2026, 4, 5, 0, 0),  # naive
                sold_price=120.0,
            )

    def test_rejects_unknown_buy_id(self, db):
        with pytest.raises(ValueError, match="does not exist"):
            record_sale(
                db,
                buy_id=99999,
                sold_at=self._bought(),
                sold_price=120.0,
            )

    def test_sold_at_equal_to_bought_at_is_allowed(self, db):
        """Selling in the same minute you bought is unusual but legal."""
        buy_id = _make_buy(db, when=self._bought())
        record_sale(
            db,
            buy_id=buy_id,
            sold_at=self._bought(),
            sold_price=100.0,
        )
        row = db.execute(
            "SELECT sold_at FROM buys WHERE id = ?", (buy_id,)
        ).fetchone()
        assert row["sold_at"] == "2026-04-01T12:00:00Z"
