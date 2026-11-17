"""DB-backed tests (run as nexora_app with RLS): request log, usage counters, endpoints, end-to-end gate."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.db import DatabaseError, transaction

from apps.accounts.models import User
from apps.ai.models import AIModelQuota, AIOrgConfig, AIRequestLog, AIUsageDaily
from apps.ai.recorder import DbRecorder, RequestRecord
from apps.ai.services import override_gemini, update_org_config
from apps.ai.testing import FakeTransport, build_engine
from apps.core.exceptions import AIPolicyBlocked
from apps.core.tenancy import tenant_context
from apps.orgs.models import Membership, Role
from apps.orgs.services import create_organization
from nexora.contracts import PayloadClass, PayloadManifest

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_a():
    owner = User.objects.create_user("owner.a@example.test", "pw-Owner-123456")
    return owner, create_organization(name="Synthetic Org A", owner=owner)


@pytest.fixture
def org_b():
    owner = User.objects.create_user("owner.b@example.test", "pw-Owner-123456")
    return owner, create_organization(name="Synthetic Org B", owner=owner)


def _rec(org_id, status="ok", **kw):
    base = dict(org_id=org_id, user_id=None, feature="nl2sql", model="gemini-3.5-flash-lite",
                prompt_key="nl2sql.generate", prompt_version="1", payload_classes=["SCHEMA"],
                payload_assets=["dataset:x"], ai_data_policy="schema_only", gemini_tier="free",
                priority="interactive", status=status, attempts=1, input_tokens=100, output_tokens=20)
    base.update(kw)
    return RequestRecord(**base)


def test_db_recorder_writes_log_and_usage(org_a):
    _, org = org_a
    with tenant_context(org.id):
        rid = DbRecorder().record(_rec(org.id))
        DbRecorder().record(_rec(org.id, input_tokens=50, output_tokens=5))
        DbRecorder().record(_rec(org.id, status="blocked_by_policy", attempts=0, input_tokens=0, output_tokens=0))
        assert AIRequestLog.objects.filter(id=rid).exists()
        [usage] = AIUsageDaily.objects.all()
        assert (usage.requests, usage.input_tokens, usage.output_tokens, usage.blocked) == (2, 150, 25, 1)


def test_request_log_is_append_only(org_a):
    _, org = org_a
    with tenant_context(org.id):
        rid = DbRecorder().record(_rec(org.id))
        with pytest.raises(DatabaseError), transaction.atomic():
            AIRequestLog.objects.filter(id=rid).update(status="error")
        with pytest.raises(DatabaseError), transaction.atomic():
            AIRequestLog.objects.filter(id=rid).delete()


def test_rls_isolates_ai_tables(org_a, org_b):
    _, a = org_a
    _, b = org_b
    with tenant_context(a.id):
        DbRecorder().record(_rec(a.id))
        AIOrgConfig.objects.create(daily_request_budget=7)
    with tenant_context(b.id):
        assert AIRequestLog.objects.count() == 0
        assert AIUsageDaily.objects.count() == 0
        assert AIOrgConfig.objects.count() == 0


def test_model_quota_is_global(org_a):
    AIModelQuota.objects.create(model="gemini-3.5-flash-lite", rpm=15, tpm=250000, rpd=1000)
    from apps.ai.config import load_model_limits

    assert load_model_limits("gemini-3.5-flash-lite").rpd == 1000
    assert load_model_limits("unknown-model").rpd == 20


def test_update_org_config_is_audited(org_a):
    from apps.core.models import AuditEvent

    _, org = org_a
    with tenant_context(org.id):
        cfg = update_org_config(daily_request_budget=80, log_prompt_text=True)
        assert cfg.daily_request_budget == 80 and cfg.log_prompt_text
        ev = AuditEvent.objects.filter(action="ai.config.update").first()
        assert ev is not None and ev.changes["daily_request_budget"] == [50, 80]
        with pytest.raises(ValueError):
            update_org_config(daily_request_budget=-1)


def test_new_org_real_dataset_blocked_end_to_end(org_a, monkeypatch, settings):
    """Default org (synthetic_only) + real dataset -> AIPolicyBlocked, zero network calls, durable log row."""
    import apps.core.registry as registry

    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    settings.GEMINI_TIER = "free"
    monkeypatch.setattr(registry, "_classifiers", {"dataset": lambda uid: "internal"})
    _, org = org_a
    from apps.ai.engine import GeminiEngine
    from apps.ai.quota import QuotaLimiter
    from apps.ai.testing import FakeRedis

    transport = FakeTransport()
    redis = FakeRedis()
    engine = GeminiEngine(transport_factory=lambda: transport, limiter=QuotaLimiter(lambda: redis))
    ds = f"dataset:{uuid4()}"
    with tenant_context(org.id):
        with pytest.raises(AIPolicyBlocked):
            engine.generate_structured(feature="nl2sql", prompt_key="nl2sql.generate", system="s", prompt="p",
                                       schema={"type": "object"}, payload=PayloadManifest({PayloadClass.SCHEMA: (ds,)}))
        log = AIRequestLog.objects.get()
        assert log.status == "blocked_by_policy" and log.ai_data_policy == "synthetic_only"
        assert log.payload_assets == [ds]
    assert transport.total_calls == 0


# ------------------------------------------------------------------------------------------ endpoints
@pytest.fixture
def fake_engine(settings):
    settings.GEMINI_API_KEY = ""
    engine, parts = build_engine()
    with override_gemini(engine):
        yield parts


def test_ai_status_endpoint(api_client, org_a, fake_engine):
    owner, org = org_a
    api_client.force_authenticate(owner)
    resp = api_client.get(f"/api/v1/orgs/{org.slug}/ai/status")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["policy"] == "synthetic_only" and body["tier"] == "free"
    assert body["configured"] is False
    assert body["org_budget"] == {"limit": 50, "used": 0}
    assert body["breaker"] == "closed" and body["resets_at"]
    assert set(body) >= {"policy", "tier", "configured", "org_budget", "breaker", "resets_at"}


def test_ai_status_reports_open_breaker(api_client, org_a, fake_engine):
    owner, org = org_a
    fake_engine["limiter"].open_day("gemini-3.5-flash-lite")
    api_client.force_authenticate(owner)
    body = api_client.get(f"/api/v1/orgs/{org.slug}/ai/status").json()
    assert body["breaker"] == "open_day"
    assert body["resets_at"] == body["models"]["fast"]["resets_at"]


def test_ai_status_other_org_is_404(api_client, org_a, org_b, fake_engine):
    owner, _ = org_a
    _, other = org_b
    api_client.force_authenticate(owner)
    assert api_client.get(f"/api/v1/orgs/{other.slug}/ai/status").status_code == 404


def test_ai_usage_endpoint_and_permissions(api_client, org_a, fake_engine):
    owner, org = org_a
    with tenant_context(org.id):
        DbRecorder().record(_rec(org.id))
        DbRecorder().record(_rec(org.id, feature="bi.narrate"))
        viewer = User.objects.create_user("viewer@example.test", "pw-Viewer-123456")
        Membership.objects.create(org_id=org.id, user=viewer, role=Role.objects.get(key="viewer"))
    api_client.force_authenticate(owner)
    resp = api_client.get(f"/api/v1/orgs/{org.slug}/ai/usage?days=7")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["totals"]["requests"] == 2 and body["range"]["days"] == 7
    assert {f["feature"] for f in body["by_feature"]} == {"nl2sql", "bi.narrate"}
    assert body["by_status"] == {"ok": 2}
    assert api_client.get(f"/api/v1/orgs/{org.slug}/ai/usage?days=0").status_code == 400
    api_client.force_authenticate(viewer)
    assert api_client.get(f"/api/v1/orgs/{org.slug}/ai/usage").status_code == 403
    assert api_client.get(f"/api/v1/orgs/{org.slug}/ai/status").status_code == 200
