"""Pacific-time helpers: Gemini RPD quotas reset at midnight America/Los_Angeles."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .constants import PACIFIC_TZ

PACIFIC = ZoneInfo(PACIFIC_TZ)


def _as_dt(now: float | datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.fromtimestamp(float(now), tz=UTC)


def pacific_day(now: float | datetime | None = None) -> date:
    """The quota day (Pacific calendar date) containing `now`."""
    return _as_dt(now).astimezone(PACIFIC).date()


def next_pacific_midnight(now: float | datetime | None = None) -> datetime:
    """The next midnight in America/Los_Angeles (DST-aware), returned in UTC."""
    local = _as_dt(now).astimezone(PACIFIC)
    nxt = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=PACIFIC)
    return nxt.astimezone(UTC)


def seconds_until_pacific_midnight(now: float | datetime | None = None) -> int:
    dt = _as_dt(now)
    return max(1, int((next_pacific_midnight(dt) - dt).total_seconds()))
