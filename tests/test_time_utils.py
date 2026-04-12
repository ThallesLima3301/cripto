"""Tests for `crypto_monitor.utils.time_utils`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from crypto_monitor.utils.time_utils import (
    UTC,
    floor_to_day,
    floor_to_hour,
    from_utc_iso,
    is_quiet_hours,
    minutes_between,
    ms_to_utc_iso,
    now_utc,
    to_utc_iso,
    utc_iso_to_ms,
)


# ---------- now_utc ----------

def test_now_utc_is_timezone_aware():
    n = now_utc()
    assert n.tzinfo is not None
    assert n.utcoffset().total_seconds() == 0


# ---------- to_utc_iso / from_utc_iso ----------

def test_to_utc_iso_appends_z():
    dt = datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)
    assert to_utc_iso(dt) == "2026-04-11T14:30:00Z"


def test_to_utc_iso_converts_non_utc_to_utc():
    from zoneinfo import ZoneInfo
    sao = ZoneInfo("America/Sao_Paulo")
    dt = datetime(2026, 4, 11, 11, 30, 0, tzinfo=sao)  # 14:30 UTC
    assert to_utc_iso(dt) == "2026-04-11T14:30:00Z"


def test_to_utc_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_utc_iso(datetime(2026, 4, 11, 14, 30, 0))


def test_from_utc_iso_with_z():
    dt = from_utc_iso("2026-04-11T14:30:00Z")
    assert dt == datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)


def test_from_utc_iso_with_explicit_offset():
    dt = from_utc_iso("2026-04-11T14:30:00+00:00")
    assert dt == datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)


def test_iso_roundtrip():
    original = datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)
    assert from_utc_iso(to_utc_iso(original)) == original


# ---------- ms <-> iso ----------

def test_ms_to_iso_unix_epoch():
    assert ms_to_utc_iso(0) == "1970-01-01T00:00:00Z"


def test_iso_to_ms_unix_epoch():
    assert utc_iso_to_ms("1970-01-01T00:00:00Z") == 0


def test_ms_iso_roundtrip():
    ms = 1700003600000  # arbitrary
    assert utc_iso_to_ms(ms_to_utc_iso(ms)) == ms


# ---------- floor helpers ----------

def test_floor_to_hour_drops_minutes_seconds():
    dt = datetime(2026, 4, 11, 14, 37, 12, 999, tzinfo=UTC)
    assert floor_to_hour(dt) == datetime(2026, 4, 11, 14, 0, 0, tzinfo=UTC)


def test_floor_to_day_returns_midnight_utc():
    dt = datetime(2026, 4, 11, 14, 37, 12, tzinfo=UTC)
    assert floor_to_day(dt) == datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)


def test_floor_to_hour_rejects_naive():
    with pytest.raises(ValueError):
        floor_to_hour(datetime(2026, 4, 11, 14, 37, 12))


# ---------- quiet hours ----------

class TestQuietHoursWrapping:
    """Default config: 22..8 local time, wraps midnight."""

    TZ = "America/Sao_Paulo"  # UTC-3 (no DST in 2026)

    def _local(self, hour: int) -> datetime:
        # Build a UTC datetime that maps to `hour` in Sao Paulo (UTC-3).
        # Use timedelta so hours 22..23 don't overflow past midnight UTC.
        base = datetime(2026, 4, 11, 0, 0, 0, tzinfo=UTC)
        return base + timedelta(hours=hour + 3)

    def test_inside_quiet_pre_midnight(self):
        # 23:00 local
        assert is_quiet_hours(self._local(23), self.TZ, 22, 8) is True

    def test_inside_quiet_post_midnight(self):
        # 02:00 local
        assert is_quiet_hours(self._local(2), self.TZ, 22, 8) is True

    def test_quiet_window_lower_boundary_is_inclusive(self):
        # 22:00 local
        assert is_quiet_hours(self._local(22), self.TZ, 22, 8) is True

    def test_quiet_window_upper_boundary_is_exclusive(self):
        # 08:00 local
        assert is_quiet_hours(self._local(8), self.TZ, 22, 8) is False

    def test_outside_quiet_during_day(self):
        # 14:00 local
        assert is_quiet_hours(self._local(14), self.TZ, 22, 8) is False


class TestQuietHoursNonWrapping:
    """Window that does not wrap midnight (e.g. 9..17 local)."""

    TZ = "UTC"

    def test_inside_window(self):
        dt = datetime(2026, 4, 11, 12, 0, 0, tzinfo=UTC)
        assert is_quiet_hours(dt, self.TZ, 9, 17) is True

    def test_before_window(self):
        dt = datetime(2026, 4, 11, 8, 59, 0, tzinfo=UTC)
        assert is_quiet_hours(dt, self.TZ, 9, 17) is False

    def test_at_end_excluded(self):
        dt = datetime(2026, 4, 11, 17, 0, 0, tzinfo=UTC)
        assert is_quiet_hours(dt, self.TZ, 9, 17) is False


def test_quiet_hours_zero_window_is_always_false():
    dt = datetime(2026, 4, 11, 14, 0, 0, tzinfo=UTC)
    assert is_quiet_hours(dt, "UTC", 8, 8) is False


def test_quiet_hours_rejects_naive_datetime():
    with pytest.raises(ValueError):
        is_quiet_hours(datetime(2026, 4, 11, 14, 0, 0), "UTC", 22, 8)


# ---------- minutes_between ----------

def test_minutes_between_basic():
    a = datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)
    b = datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC)
    assert minutes_between(a, b) == 90.0


def test_minutes_between_is_absolute():
    a = datetime(2026, 4, 11, 13, 0, 0, tzinfo=UTC)
    b = datetime(2026, 4, 11, 14, 30, 0, tzinfo=UTC)
    assert minutes_between(a, b) == 90.0
