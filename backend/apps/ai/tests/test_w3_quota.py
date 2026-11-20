"""W3 quota UX: the no-Redis counter store, the /ai/status `quota` block, the once-a-day admin notification and the
database-free engine used by developer tools. No database, no Gemini (FakeTransport / in-memory stores)."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from apps.ai import engine as engine_mod
from apps.ai import notify, quota_store
from apps.ai import services as ai_services
from apps.ai.config import OrgAIConfig
from apps.ai.constants import RequestStatus
from apps.ai.exceptions import AIPolicyBlocked, AIQuotaExhausted
from apps.ai.quota import ModelLimits, OrgLimits, QuotaLimiter
from apps.ai.quota_store import DbRedis, MemoryCounterBackend
from apps.ai.testing import FakeClock, FakeTransport, build_engine
from apps.ai.timeutil import next_pacific_midnight
from nexora.contracts import PayloadClass, PayloadManifest

T0 = datetime(2026, 11, 17, 18, 0, 30, tzinfo=UTC).timestamp()  # 10:00:30 PST
MODEL = "gemini-3.5-flash-lite"
RESET = "2026-11-18T08:00:00+00:00"


# ------------------------------------------------------------------------------------------- DbRedis
@pytest.fixture
def store():
    clock = FakeClock(T0)
    return DbRedis(MemoryCounterBackend(), clock=clock), clock


def test_set_get_expiry_and_nx(store):
    r, clock = store
    assert r.get("k") is None
    assert r.set("k", "v", ex=10) is True and r.get("k") == b"v"
    assert r.set("k", "w", nx=True) is None and r.get("k") == b"v"
    clock.advance(11)
    assert r.get("k") is None  # expired
    assert r.set("k", "w", nx=True) is True
    assert r.set("e", 1, exat=int(T0) + 100) is True
    clock.now = T0 + 101
    assert r.mget(["e", "k"]) == [None, b"w"]


def test_incr_expire_delete(store):
    r, clock = store
    assert r.incrby("c", 5) == 5 and r.incr("c") == 6
    assert r.expire("missing", 10) is False
    assert r.expire("c", 10) is True
    assert r.delete("c", "missing") == 1 and r.get("c") is None


def test_transaction_is_all_or_nothing(store):
    r, _ = store

    def ok(pipe):
        assert pipe.mget(["a", "b"]) == [None, None]
        pipe.multi()
        pipe.incrby("a", 2).expire("a", 60)
        pipe.incrby("b", 3)

    assert r.transaction(ok, "a", "b") == [2, True, 3]

    def refuse(pipe):
        pipe.multi()
        pipe.incrby("a", 100)
        raise RuntimeError("refused inside the transaction")

    with pytest.raises(RuntimeError):
        r.transaction(refuse, "a", "b")
    assert r.mget(["a", "b"]) == [b"2", b"3"]  # nothing of the refused transaction was applied

    def sneaky(pipe):
        pipe.incrby("unwatched", 1)

    with pytest.raises(KeyError):
        r.transaction(sneaky, "a")


def test_reads_never_create_rows_and_purge_drops_expired(store):
    r, clock = store
    r.mget(["x", "y"])
    assert r.backend._data == {}  # reporting reads do not insert placeholders
    r.set("live", "1", ex=1000)
    r.set("old", "1", ex=5)
    clock.advance(10)
    assert r.purge_expired() == 1
    assert set(r.backend._data) == {"live"}


def test_default_limiter_follows_redis_url(settings, monkeypatch):
    settings.REDIS_URL = ""
    lim = engine_mod._default_limiter()
    assert isinstance(lim._redis(), DbRedis)
    sentinel = object()
    fake_jobs = types.SimpleNamespace(redis_client=lambda: sentinel)
    monkeypatch.setitem(sys.modules, "apps.core.jobs", fake_jobs)
    settings.REDIS_URL = "redis://localhost:6379/0"
    assert engine_mod._default_limiter()._redis() is sentinel


def test_purge_job_registration(settings, monkeypatch):
    seen = []
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", types.SimpleNamespace(
        register_periodic=lambda name, every, fn: seen.append((name, every, fn))))
    settings.REDIS_URL = ""
    assert quota_store.register_periodic_job() is True
    assert seen == [("ai.purge_quota_counters", 3600, quota_store.purge_expired_counters)]
    settings.REDIS_URL = "redis://localhost:6379/0"
    assert quota_store.register_periodic_job() is False  # Redis keys expire by themselves
    settings.REDIS_URL = ""
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", None)
    assert quota_store.register_periodic_job() is False


def test_mark_once_and_project_requests(store):
    r, clock = store
    lim = QuotaLimiter(lambda: r, clock=clock)
    org = uuid4()
    assert lim.mark_once("quota_exhausted_notified", org_id=org) is True
    assert lim.mark_once("quota_exhausted_notified", org_id=org) is False
    assert lim.mark_once("quota_exhausted_notified", org_id=uuid4()) is True  # per org
    clock.now = next_pacific_midnight(T0).timestamp() + 1
    assert lim.mark_once("quota_exhausted_notified", org_id=org) is True  # a new Pacific day
    for _ in range(3):
        lim.reserve(org_id=org, user_id=None, model=MODEL, priority="interactive", est_tokens=10,
                    model_limits=ModelLimits(1000, 10**7, 10**5), org_limits=OrgLimits())
    assert lim.project_day_requests(MODEL) == 3


# ------------------------------------------------------------------------------------------- quota_state
def qs(**kw):
    base = {"used": 0, "limit": 50, "budget_resets_at": RESET}
    return ai_services.quota_state(**{**base, **kw})


def test_quota_state_ok_low_exhausted():
    assert qs(used=10) == {"state": "ok", "resets_at": RESET, "used_today": 10, "limit_today": 50,
                           "remaining_today": 40, "scope": None, "rate_limited": False}
    low = qs(used=40)  # 10 of 50 left = 20%
    assert (low["state"], low["scope"]) == ("low", "org_requests")
    assert qs(used=39)["state"] == "ok"
    out = qs(used=50)
    assert (out["state"], out["scope"], out["remaining_today"]) == ("exhausted", "org_requests", 0)
    assert qs(limit=0)["state"] == "exhausted"  # a zero budget means AI is off for the org


def test_quota_state_other_budgets_and_breakers():
    assert qs(project_used=18, project_limit=18)["scope"] == "platform"
    assert qs(project_used=15, project_limit=18)["scope"] == "platform"  # low
    assert qs(project_used=15, project_limit=18)["state"] == "low"
    tok = qs(tokens_used=250_000, tokens_limit=250_000)
    assert (tok["state"], tok["scope"]) == ("exhausted", "org_tokens")
    user = qs(user_used=20, user_limit=20)
    assert (user["state"], user["scope"]) == ("exhausted", "user")
    assert qs(user_used=None, user_limit=20)["state"] == "ok"  # no viewer: no personal cap
    day = qs(breaker="open_day", breaker_resets_at="2026-11-18T08:00:05+00:00")
    assert (day["state"], day["scope"], day["resets_at"]) == ("exhausted", "platform", "2026-11-18T08:00:05+00:00")
    minute = qs(breaker="open_minute", breaker_resets_at="2026-11-17T18:01:00+00:00")
    assert minute["state"] == "ok" and minute["rate_limited"] is True and minute["resets_at"] == RESET


def test_ai_status_reports_the_quota_block(monkeypatch, settings):
    from apps.ai import config as cfgmod
    from apps.core.tenancy.context import current_actor, current_org

    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = MODEL
    settings.REDIS_URL = ""
    monkeypatch.setattr(ai_services, "_policy_state", lambda: {"policy": "synthetic_only", "consent_valid": False})
    monkeypatch.setattr(cfgmod, "load_org_config", lambda org_id=None: OrgAIConfig(daily_request_budget=5))
    monkeypatch.setattr(cfgmod, "load_model_limits", lambda model: ModelLimits(1000, 10**7, 10**5))
    engine, parts = build_engine(org_config=OrgAIConfig(daily_request_budget=5), clock=FakeClock(T0))
    org, user = uuid4(), uuid4()
    for _ in range(4):
        parts["limiter"].reserve(org_id=org, user_id=user, model=MODEL, priority="interactive", est_tokens=10,
                                 model_limits=ModelLimits(1000, 10**7, 10**5), org_limits=OrgLimits(daily_requests=5))
    t1, t2 = current_org.set(org), current_actor.set(("user", user))
    try:
        with ai_services.override_gemini(engine):
            status = ai_services.ai_status(user_id=user)
    finally:
        current_actor.reset(t2)
        current_org.reset(t1)
    quota = status["quota"]
    assert (quota["state"], quota["scope"], quota["used_today"], quota["limit_today"]) == ("low", "org_requests", 4, 5)
    assert quota["user"] == {"used": 4, "limit": 20} and quota["resets_at"]
    assert status["configured"] is True and status["tier"] == "free" and status["limiter_backend"] == "db"
    assert status["embeddings"]["available"] in (True, False)


# ------------------------------------------------------------------------------------------- notifications
@pytest.fixture
def notifications(monkeypatch):
    sent = []
    module = types.SimpleNamespace(
        notify_permission=lambda codename, kind, title, body, link: sent.append((codename, kind, title, body, link))
        or 2)
    monkeypatch.setitem(sys.modules, "apps.core.notifications", module)
    return sent


def test_quota_message_text():
    title, body = notify.quota_message("org_day", datetime(2026, 11, 18, 8, 0, tzinfo=UTC))
    assert title == "AI quota exhausted for today"
    assert body == ("Your organization's daily AI request budget is used up. It resets at 2026-11-18 08:00 UTC. "
                    "Simple questions, saved queries, dashboards and extractive document answers keep working.")


def test_first_org_exhaustion_of_the_day_notifies_admins_once(org_ctx, gemini_key, notifications):
    responses = [FakeTransport.json({"answer": "ok"}) for _ in range(3)]
    engine, parts = build_engine(transport=FakeTransport(responses), org_config=OrgAIConfig(daily_request_budget=1))
    payload = PayloadManifest({PayloadClass.SCHEMA: ("dataset:x",)})

    def call():
        return engine.generate_structured(feature="nl2sql", prompt_key="adhoc", system="s", prompt="p",
                                          schema={"type": "object"}, payload=payload)

    call()
    for _ in range(2):
        with pytest.raises(AIQuotaExhausted) as exc:
            call()
        assert exc.value.extra["resets_at"] and exc.value.extra["scope"] == "org_day"
    assert len(notifications) == 1
    codename, kind, title, body, link = notifications[0]
    assert (codename, kind, link) == ("org.settings.edit", "ai.quota_exhausted", "/settings/ai-policy")
    assert "daily AI request budget" in body
    parts["clock"].now = next_pacific_midnight(parts["clock"]()).timestamp() + 1
    call()
    call_failed = pytest.raises(AIQuotaExhausted)
    with call_failed:
        call()
    assert len(notifications) == 2  # the next Pacific day notifies again


def test_a_personal_cap_does_not_notify_admins(org_ctx, gemini_key, notifications):
    engine, _ = build_engine(transport=FakeTransport([FakeTransport.json({"a": 1})]),
                             org_config=OrgAIConfig(per_user_daily_requests=1))
    payload = PayloadManifest({PayloadClass.SCHEMA: ("dataset:x",)})
    kw = {"feature": "nl2sql", "prompt_key": "adhoc", "system": "s", "prompt": "p", "schema": {"type": "object"},
          "payload": payload}
    engine.generate_structured(**kw)
    with pytest.raises(AIQuotaExhausted) as exc:
        engine.generate_structured(**kw)
    assert exc.value.extra["scope"] == "user_day"
    assert notifications == []


def test_notify_falls_back_to_member_lookup_and_never_raises(monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "apps.core.notifications", types.SimpleNamespace(
        notify=lambda users, kind, title, body, link: sent.append((tuple(users), kind, link)) or len(users)))
    from apps.orgs import services as org_services

    admin = uuid4()
    monkeypatch.setattr(org_services, "members_with_permission", lambda codename: [admin], raising=False)
    assert notify.notify_quota_exhausted("org_tokens", None) is True
    assert sent == [((admin,), "ai.quota_exhausted", "/settings/ai-policy")]

    def broken(*a, **k):
        raise RuntimeError("notifications table missing")

    monkeypatch.setitem(sys.modules, "apps.core.notifications", types.SimpleNamespace(notify_permission=broken))
    assert notify.notify_quota_exhausted("org_day", None) is False
    monkeypatch.setitem(sys.modules, "apps.core.notifications", None)  # not built yet: skipped
    assert notify.notify_quota_exhausted("org_day", None) is False


# ------------------------------------------------------------------------------------------- standalone engine
def test_standalone_engine_runs_the_real_policy_gate_without_a_database(org_ctx, gemini_key):
    payload = PayloadManifest({PayloadClass.SCHEMA: ("dataset:golden",)})
    kw = {"feature": "nl2sql", "prompt_key": "adhoc", "system": "s", "prompt": "p", "schema": {"type": "object"},
          "payload": payload}
    transport = FakeTransport([FakeTransport.json({"sql": "SELECT 1"})])
    eng = ai_services.standalone_engine(transport_factory=lambda: transport, sleep=lambda s: None)
    assert eng.generate_structured(**kw) == {"sql": "SELECT 1"}
    assert [r.status for r in eng.recorder.records] == [RequestStatus.OK.value]

    blocked = FakeTransport()
    internal = ai_services.standalone_engine(transport_factory=lambda: blocked, classification="internal")
    with pytest.raises(AIPolicyBlocked):
        internal.generate_structured(**kw)
    assert blocked.total_calls == 0  # refused before any network call
