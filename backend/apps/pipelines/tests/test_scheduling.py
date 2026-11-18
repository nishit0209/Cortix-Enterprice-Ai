"""Cron validation and next-fire computation (pure)."""

import datetime as dt

import pytest
from rest_framework.exceptions import ValidationError

from apps.pipelines import scheduling


def test_validate_cron_accepts_five_fields_and_normalizes_spaces():
    assert scheduling.validate_cron("0  2 * * *", "Asia/Kolkata") == "0 2 * * *"


@pytest.mark.parametrize("expr", ["", "* * *", "0 0 * * * *", "61 * * * *", "nonsense here x y z", "* * * * *"])
def test_validate_cron_rejects_invalid_or_too_frequent(expr):
    with pytest.raises(ValidationError):
        scheduling.validate_cron(expr, "UTC")


def test_unknown_timezone():
    with pytest.raises(ValidationError):
        scheduling.validate_cron("0 2 * * *", "Mars/Olympus")


def test_next_fire_in_timezone_returns_utc():
    after = dt.datetime(2026, 11, 18, 0, 0, tzinfo=dt.UTC)
    nxt = scheduling.next_fire("30 9 * * *", "Asia/Kolkata", after)  # 09:30 IST = 04:00 UTC
    assert nxt == dt.datetime(2026, 11, 18, 4, 0, tzinfo=dt.UTC)
    assert scheduling.next_fire("30 9 * * *", "Asia/Kolkata", nxt) == dt.datetime(2026, 11, 19, 4, 0, tzinfo=dt.UTC)


def test_next_fire_is_dst_aware():
    # New York switches to DST on 2026-03-08: 09:00 local is 14:00 UTC before and 13:00 UTC after
    before = scheduling.next_fire("0 9 * * *", "America/New_York", dt.datetime(2026, 3, 7, 0, 0, tzinfo=dt.UTC))
    after = scheduling.next_fire("0 9 * * *", "America/New_York", dt.datetime(2026, 3, 9, 0, 0, tzinfo=dt.UTC))
    assert before.hour == 14 and after.hour == 13


def test_min_interval_setting(settings):
    settings.PIPELINE_MIN_SCHEDULE_INTERVAL_S = 60
    assert scheduling.validate_cron("* * * * *", "UTC") == "* * * * *"
