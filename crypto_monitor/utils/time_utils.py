"""Time helpers.

All timestamps inside crypto_monitor are stored as UTC ISO 8601 strings with
a trailing 'Z' (e.g. '2026-04-10T14:30:00Z'). Local time is only used at
visible edges — quiet-hours decisions and notification formatting — never
for storage.

Keeping all time conversion in one module makes daylight-saving-time bugs,
naive-datetime bugs, and timezone drift easy to audit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc_iso(dt: datetime) -> str:
    """Serialize a timezone-aware datetime to UTC ISO 8601 with a 'Z' suffix."""
    if dt.tzinfo is None:
        raise ValueError("to_utc_iso requires a timezone-aware datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_utc_iso(s: str) -> datetime:
    """Parse a UTC ISO 8601 string (with 'Z' or '+00:00') back into a datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_utc_iso(ms: int) -> str:
    """Convert a Binance millisecond epoch timestamp to a UTC ISO string."""
    return to_utc_iso(datetime.fromtimestamp(ms / 1000, tz=UTC))


def utc_iso_to_ms(s: str) -> int:
    """Convert a UTC ISO string to a millisecond epoch (for Binance requests)."""
    return int(from_utc_iso(s).timestamp() * 1000)


def floor_to_hour(dt: datetime) -> datetime:
    """Truncate a UTC datetime to the start of its hour."""
    if dt.tzinfo is None:
        raise ValueError("floor_to_hour requires a timezone-aware datetime")
    dt = dt.astimezone(UTC)
    return dt.replace(minute=0, second=0, microsecond=0)


def floor_to_day(dt: datetime) -> datetime:
    """Truncate a UTC datetime to the start of its day (00:00 UTC)."""
    if dt.tzinfo is None:
        raise ValueError("floor_to_day requires a timezone-aware datetime")
    dt = dt.astimezone(UTC)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def is_quiet_hours(
    now: datetime,
    tz_name: str,
    start_hour: int,
    end_hour: int,
) -> bool:
    """Return True if `now` falls inside the configured local quiet-hours window.

    Quiet hours wrap across midnight when `start_hour > end_hour`
    (e.g. 22..8 means 22:00-23:59 AND 00:00-07:59 local).

    `now` must be timezone-aware; it's converted to the configured timezone
    for the hour check, so DST transitions are handled by `zoneinfo`.
    """
    if now.tzinfo is None:
        raise ValueError("is_quiet_hours requires a timezone-aware datetime")
    if start_hour == end_hour:
        return False
    local_hour = now.astimezone(ZoneInfo(tz_name)).hour
    if start_hour < end_hour:
        return start_hour <= local_hour < end_hour
    return local_hour >= start_hour or local_hour < end_hour


def minutes_between(a: datetime, b: datetime) -> float:
    """Return the absolute number of minutes between two timezone-aware datetimes."""
    if a.tzinfo is None or b.tzinfo is None:
        raise ValueError("minutes_between requires timezone-aware datetimes")
    delta: timedelta = a - b
    return abs(delta.total_seconds()) / 60.0
