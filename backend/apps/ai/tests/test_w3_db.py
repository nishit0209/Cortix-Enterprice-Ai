"""W3 apps.ai on PostgreSQL: the no-Redis quota counter table and the /ai/status `quota` block (nexora_app, RLS)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.accounts.models import User
from apps.ai.models import AIQuotaCounter
from apps.ai.quota import ModelLimits, OrgLimits, QuotaDenied, QuotaLimiter
from apps.ai.quota_store import DbRedis, OrmCounterBackend
from apps.ai.services import override_gemini, update_org_config
from apps.ai.testing import build_engine
from apps.core.tenancy import tenant_context
from apps.orgs.services import create_organization

pytestmark = pytest.mark.django_db

MODEL = "gemini-3.5-flash-lite"


@pytest.fixture
def org():
    owner = User.objects.create_user("w3.quota.owner@example.test", "pw-Owner-123456")
    return owner, create_organization(name="W3 Quota Org", owner=owner)


def test_counter_table_backs_the_limiter():
    store = DbRedis(OrmCounterBackend("default"))
    lim = QuotaLimiter(lambda: store)
    org_id = uuid4()
    for _ in range(3):
        lim.reserve(org_id=org_id, user_id=None, model=MODEL, priority="interactive", est_tokens=50,
                    model_limits=ModelLimits(1000, 10**7, 10**5), org_limits=OrgLimits(daily_requests=3))
    with pytest.raises(QuotaDenied) as exc:
        lim.reserve(org_id=org_id, user_id=None, model=MODEL, priority="interactive", est_tokens=50,
                    model_limits=ModelLimits(1000, 10**7, 10**5), org_limits=OrgLimits(daily_requests=3))
    assert exc.value.denial.scope == "org_day"
    usage = lim.usage(org_id)
    assert (usage.org_requests, usage.org_tokens) == (3, 150)
    assert lim.project_day_requests(MODEL) == 3
    # the refused reservation left nothing behind (its transaction rolled back)
    org_rows = AIQuotaCounter.objects.filter(key__contains=str(org_id))
    assert sorted(int(r.value) for r in org_rows if r.key.endswith(":req")) == [3]
    assert all(r.expires_at is not None for r in org_rows)
    assert lim.mark_once("quota_exhausted_notified", org_id=org_id) is True
    assert lim.mark_once("quota_exhausted_notified", org_id=org_id) is False
    lim.open_day(MODEL)
    assert lim.breaker(MODEL).state == "open_day"
    lim.close(MODEL)
    assert lim.breaker(MODEL).state == "closed"


def test_purge_removes_expired_and_placeholder_rows():
    from datetime import timedelta

    from django.utils import timezone

    AIQuotaCounter.objects.create(key="ai:q:test:expired", value="1", expires_at=timezone.now() - timedelta(hours=1))
    AIQuotaCounter.objects.create(key="ai:q:test:placeholder", value=None)
    AIQuotaCounter.objects.create(key="ai:q:test:live", value="1", expires_at=timezone.now() + timedelta(hours=1))
    assert DbRedis(OrmCounterBackend("default")).purge_expired() == 2
    assert list(AIQuotaCounter.objects.filter(key__startswith="ai:q:test:").values_list("key", flat=True)) == [
        "ai:q:test:live"]


def test_ai_status_quota_block(api_client, org, settings):
    owner, o = org
    settings.GEMINI_API_KEY = ""
    engine, parts = build_engine()
    with tenant_context(o.id):
        update_org_config(daily_request_budget=5)
    for _ in range(5):
        parts["limiter"].reserve(org_id=o.id, user_id=owner.pk, model=MODEL, priority="interactive", est_tokens=10,
                                 model_limits=ModelLimits(1000, 10**7, 10**5), org_limits=OrgLimits(daily_requests=5))
    api_client.force_authenticate(owner)
    with override_gemini(engine):
        body = api_client.get(f"/api/v1/orgs/{o.slug}/ai/status").json()
    quota = body["quota"]
    assert {"state", "resets_at", "used_today", "limit_today"} <= set(quota)
    assert (quota["state"], quota["scope"], quota["used_today"], quota["limit_today"]) == (
        "exhausted", "org_requests", 5, 5)
    assert quota["resets_at"] == body["budget_resets_at"]
    assert body["configured"] is False and body["tier"] == "free"
    assert body["embeddings"]["available"] in (True, False)
