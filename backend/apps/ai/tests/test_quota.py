"""Quota limiter + circuit breaker on an in-memory Redis with a fake clock."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from apps.ai.quota import (
    LimiterUnavailable,
    ModelLimits,
    OrgLimits,
    QuotaDenied,
    QuotaLimiter,
    default_model_limits,
)
from apps.ai.quota_store import DbRedis, MemoryCounterBackend
from apps.ai.testing import FakeClock, FakeRedis
from apps.ai.timeutil import next_pacific_midnight

MODEL = "gemini-3.5-flash-lite"
# 2026-11-17 18:00:30 UTC == 10:00:30 PST
T0 = datetime(2026, 11, 17, 18, 0, 30, tzinfo=UTC).timestamp()


class FlakyCounterBackend(MemoryCounterBackend):
    """The PostgreSQL counter store's semantics in memory, with an outage switch like FakeRedis.fail."""

    fail = False

    def read(self, keys):
        if self.fail:
            raise ConnectionError("database unreachable")
        return super().read(keys)

    @contextmanager
    def locked(self, keys):
        if self.fail:
            raise ConnectionError("database unreachable")
        with super().locked(keys) as snap:
            yield snap


@pytest.fixture(params=["redis", "db"])
def env(request):
    """Every scenario runs on Redis (FakeRedis) AND on the no-Redis PostgreSQL store (DbRedis semantics)."""
    clock = FakeClock(T0)
    if request.param == "redis":
        redis = FakeRedis(clock)
        return QuotaLimiter(lambda: redis, clock=clock), clock, redis
    backend = FlakyCounterBackend()
    store = DbRedis(backend, clock=clock)
    return QuotaLimiter(lambda: store, clock=clock), clock, backend


BIG = ModelLimits(rpm=1000, tpm=10_000_000, rpd=100_000)
ORG = uuid4()


def reserve(lim, *, user=None, priority="interactive", est=100, model_limits=BIG, org_limits=OrgLimits(), org=ORG):
    return lim.reserve(org_id=org, user_id=user, model=MODEL, priority=priority, est_tokens=est,
                       model_limits=model_limits, org_limits=org_limits)


def test_org_daily_budget(env):
    lim, _, _ = env
    for _ in range(5):
        reserve(lim, org_limits=OrgLimits(daily_requests=5))
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, org_limits=OrgLimits(daily_requests=5))
    d = exc.value.denial
    assert (d.scope, d.code) == ("org_day", "ai_quota_exhausted")
    assert d.resets_at == next_pacific_midnight(T0)
    assert lim.usage(ORG).org_requests == 5


def test_org_budget_is_per_org(env):
    lim, _, _ = env
    reserve(lim, org_limits=OrgLimits(daily_requests=1))
    reserve(lim, org=uuid4(), org_limits=OrgLimits(daily_requests=1))  # another org is unaffected


def test_per_user_cap(env):
    lim, _, _ = env
    alice, bob = uuid4(), uuid4()
    limits = OrgLimits(daily_requests=100, per_user_daily_requests=2)
    reserve(lim, user=alice, org_limits=limits)
    reserve(lim, user=alice, org_limits=limits)
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, user=alice, org_limits=limits)
    assert exc.value.denial.scope == "user_day"
    reserve(lim, user=bob, org_limits=limits)
    assert lim.usage(ORG, alice).user_requests == 2


@pytest.mark.parametrize(("priority", "allowed"), [("interactive", 10), ("assistive", 6), ("background", 3)])
def test_priority_shares_of_org_budget(env, priority, allowed):
    lim, _, _ = env
    limits = OrgLimits(daily_requests=10, per_user_daily_requests=1000)
    for _ in range(allowed):
        reserve(lim, priority=priority, org_limits=limits)
    with pytest.raises(QuotaDenied):
        reserve(lim, priority=priority, org_limits=limits)


def test_background_cannot_consume_whole_project_rpd(env):
    lim, _, _ = env
    ml = ModelLimits(rpm=1000, tpm=10_000_000, rpd=100)  # effective 90 after the 10% margin
    org = OrgLimits(daily_requests=10_000, daily_tokens=10**9, per_user_daily_requests=10_000)
    for _ in range(27):  # 30% of 90
        reserve(lim, priority="background", model_limits=ml, org_limits=org)
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, priority="background", model_limits=ml, org_limits=org)
    assert exc.value.denial.scope == "project_day_priority"
    assert lim.breaker(MODEL).state == "closed"  # a priority denial must not open the breaker for everyone
    reserve(lim, priority="interactive", model_limits=ml, org_limits=org)


def test_project_rpd_exhaustion_opens_day_breaker(env):
    lim, clock, _ = env
    ml = ModelLimits(rpm=1000, tpm=10_000_000, rpd=10)  # effective 9
    org = OrgLimits(daily_requests=1000, daily_tokens=10**9, per_user_daily_requests=1000)
    for _ in range(9):
        reserve(lim, model_limits=ml, org_limits=org)
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, model_limits=ml, org_limits=org)
    assert exc.value.denial.scope == "project_day"
    br = lim.breaker(MODEL)
    assert br.state == "open_day"
    assert br.resets_at == next_pacific_midnight(T0)
    # 2026-11-18 00:00 PST == 08:00 UTC
    assert br.resets_at == datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    clock.now = br.resets_at.timestamp() + 1
    assert lim.breaker(MODEL).state == "closed"
    reserve(lim, model_limits=ml, org_limits=org)  # new Pacific day, counters reset


def test_project_rpm_with_safety_margin(env):
    lim, clock, _ = env
    ml = ModelLimits(rpm=10, tpm=10_000_000, rpd=10_000)  # effective 9 per minute
    org = OrgLimits(daily_requests=1000, daily_tokens=10**9, per_user_daily_requests=1000)
    for _ in range(9):
        reserve(lim, model_limits=ml, org_limits=org)
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, model_limits=ml, org_limits=org)
    assert (exc.value.denial.scope, exc.value.denial.code) == ("project_minute", "ai_rate_limited")
    assert exc.value.denial.retry_after_s == 30
    clock.advance(31)
    reserve(lim, model_limits=ml, org_limits=org)


def test_tpm_limit(env):
    lim, _, _ = env
    ml = ModelLimits(rpm=1000, tpm=1000, rpd=10_000)  # effective 900 tokens/min
    org = OrgLimits(daily_requests=1000, daily_tokens=10**9, per_user_daily_requests=1000)
    reserve(lim, est=800, model_limits=ml, org_limits=org)
    with pytest.raises(QuotaDenied):
        reserve(lim, est=200, model_limits=ml, org_limits=org)


def test_org_token_budget_and_reconcile(env):
    lim, _, _ = env
    org = OrgLimits(daily_requests=1000, daily_tokens=1000, per_user_daily_requests=1000)
    r = reserve(lim, est=600, org_limits=org)
    lim.reconcile(r, 300)  # actual usage lower than estimated
    assert lim.usage(ORG).org_tokens == 300
    reserve(lim, est=600, org_limits=org)
    with pytest.raises(QuotaDenied) as exc:
        reserve(lim, est=200, org_limits=org)
    assert exc.value.denial.scope == "org_tokens"


def test_retry_reservation_counts_project_only(env):
    lim, _, _ = env
    lim.reserve(org_id=ORG, user_id=uuid4(), model=MODEL, priority="interactive", est_tokens=0,
                model_limits=BIG, org_limits=None)
    assert lim.usage(ORG).org_requests == 0


def test_minute_breaker_and_day_breaker_precedence(env):
    lim, clock, _ = env
    st = lim.open_minute(MODEL, 13)
    assert st.state == "open_minute"
    assert lim.breaker(MODEL).resets_at.timestamp() == pytest.approx(T0 + 13)
    lim.open_day(MODEL)
    lim.open_minute(MODEL, 5)  # must not shorten an open_day breaker
    assert lim.breaker(MODEL).state == "open_day"
    lim.close(MODEL)
    assert lim.breaker(MODEL).state == "closed"


def test_redis_outage_fails_closed(env):
    lim, _, redis = env
    redis.fail = True
    with pytest.raises(LimiterUnavailable):
        reserve(lim)
    with pytest.raises(LimiterUnavailable):
        lim.breaker(MODEL)


def test_pacific_day_boundary_resets_counters(env):
    lim, clock, _ = env
    reserve(lim, org_limits=OrgLimits(daily_requests=1))
    clock.now = next_pacific_midnight(T0).timestamp() + 5
    reserve(lim, org_limits=OrgLimits(daily_requests=1))


def test_default_limits_are_conservative():
    assert default_model_limits("gemini-3.8-flash").rpd == 20
    assert default_model_limits("gemini-3.5-flash-lite").rpd == 500
    assert ModelLimits(10, 100, 20).effective() == ModelLimits(9, 90, 18)
