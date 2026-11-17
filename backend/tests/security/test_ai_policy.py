"""AI data policy (TESTING_STRATEGY §4.3, P2-AC3, AI_ARCHITECTURE §5).

Runs now against what exists (service, DB default, CHECK constraint, ai_policy_state contract) and
automatically starts exercising the HTTP endpoints / the AI gate once the owning agents merge them
(each such test skips with a clear reason until its route or function exists).
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction

from apps.core.tenancy import tenant_context
from testing.client import api_client_for, route_exists
from testing.db import clear_db_tenant, db_tenant
from testing.factories import TEST_PASSWORD, set_org_fields

pytestmark = [pytest.mark.django_db, pytest.mark.ac("P2-AC3")]


def _org_policy(org_id) -> str:
    with db_tenant(org_id), connection.cursor() as cur:
        cur.execute("SELECT ai_data_policy FROM orgs_organization WHERE id = %s", [str(org_id)])
        return cur.fetchone()[0]


def _require_route(path: str):
    if not route_exists(path):
        pytest.skip(f"{path} not implemented yet (Backend agent, wave 1)")


def _code(resp) -> str | None:
    try:
        return resp.json().get("code")
    except ValueError:
        return None


# ----------------------------------------------------------------------------------- creation paths
def test_service_created_orgs_are_synthetic_only(two_orgs):
    for world in two_orgs:
        assert world.org.ai_data_policy == "synthetic_only"
        assert _org_policy(world.id) == "synthetic_only"


def test_service_ignores_attempts_to_pick_a_policy(make_user):
    from apps.orgs.services import create_organization

    params = inspect.signature(create_organization).parameters
    assert "ai_data_policy" not in params, "org creation must not accept a policy"
    org = create_organization(name="Policy Probe", owner=make_user())
    clear_db_tenant()
    assert _org_policy(org.id) == "synthetic_only"


def test_database_default_is_synthetic_only_on_raw_insert(db):
    """A raw INSERT that omits the column (e.g. a future migration or platform script) still gets the default."""
    org_id = uuid.uuid4()
    with db_tenant(org_id), connection.cursor() as cur:
        cur.execute(
            "INSERT INTO orgs_organization (id, name, slug, short_id, status, plan, region, settings, "
            "created_at, updated_at) VALUES (%s, 'Raw Insert Org', %s, %s, 'active', 'free', 'in', '{}', now(), now())",
            [str(org_id), f"raw-{org_id.hex[:8]}", org_id.hex[:8]],
        )
        cur.execute("SELECT ai_data_policy FROM orgs_organization WHERE id = %s", [str(org_id)])
        assert cur.fetchone()[0] == "synthetic_only"


def test_column_default_is_declared_in_the_schema(db):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'orgs_organization' AND column_name = 'ai_data_policy'"
        )
        assert "synthetic_only" in (cur.fetchone()[0] or "")


def test_unknown_policy_values_are_rejected_by_the_database(two_orgs):
    a = two_orgs.a
    with db_tenant(a.id):
        with pytest.raises((IntegrityError, DatabaseError)), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE orgs_organization SET ai_data_policy = 'everything' WHERE id = %s", [str(a.id)])


def test_signup_api_creates_synthetic_only_org(anon_client):
    _require_route("/api/v1/auth/signup")
    resp = anon_client.post(
        "/api/v1/auth/signup",
        {"email": "signup-policy@example.test", "password": TEST_PASSWORD, "full_name": "Signup Policy",
         "org_name": "Signup Policy Org", "ai_data_policy": "full"},  # extra field must be ignored
        format="json",
    )
    assert resp.status_code == 201, resp.content
    org = resp.json()["org"]
    assert org.get("ai_data_policy", "synthetic_only") == "synthetic_only"
    assert _org_policy(org["id"]) == "synthetic_only"


def test_create_org_api_creates_synthetic_only_org(two_orgs):
    _require_route("/api/v1/orgs")
    client = api_client_for(two_orgs.a.owner)
    resp = client.post("/api/v1/orgs", {"name": "Second Org", "ai_data_policy": "full"}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["ai_data_policy"] == "synthetic_only"
    assert _org_policy(body["id"]) == "synthetic_only"


def test_org_patch_cannot_change_the_policy(two_orgs):
    a = two_orgs.a
    _require_route(f"/api/v1/orgs/{a.slug}")
    resp = api_client_for(a.owner).patch(f"/api/v1/orgs/{a.slug}", {"ai_data_policy": "full"}, format="json")
    assert resp.status_code in (200, 400)
    assert _org_policy(a.id) == "synthetic_only"


# ----------------------------------------------------------------------------------- policy state contract
def test_ai_policy_state_contract_default(two_orgs):
    from apps.orgs.services import ai_policy_state

    with tenant_context(two_orgs.a.id):
        state = ai_policy_state()
    clear_db_tenant()
    assert state["policy"] == "synthetic_only"
    assert set(state) >= {"policy", "consent_valid"}


@pytest.mark.parametrize("policy", ["schema_only", "aggregates_only", "full"])
def test_manual_db_edit_without_consent_is_not_effective(two_orgs, policy):
    from apps.orgs.services import ai_policy_state

    a = two_orgs.a
    set_org_fields(a.org, ai_data_policy=policy)
    with tenant_context(a.id):
        state = ai_policy_state()
    clear_db_tenant()
    assert state["consent_valid"] is False, "a policy above synthetic_only needs a recorded, current consent"


# ----------------------------------------------------------------------------------- consent API
def _consents_url(world) -> str:
    return f"/api/v1/orgs/{world.slug}/ai-policy/consents"


def _consent_body(policy="schema_only", **over):
    from django.conf import settings

    body = {"policy": policy, "consent_text_version": settings.AI_CONSENT_TEXT_VERSION, "acknowledged": True,
            "password": TEST_PASSWORD}
    body.update(over)
    return body


@pytest.mark.parametrize("policy", ["aggregates_only", "full"])
def test_free_tier_refuses_aggregates_and_full(two_orgs, settings, policy):
    a = two_orgs.a
    _require_route(_consents_url(a))
    settings.GEMINI_TIER = "free"
    resp = api_client_for(a.owner).post(_consents_url(a), _consent_body(policy), format="json")
    assert 400 <= resp.status_code < 500
    assert _code(resp) == "ai_policy_tier_forbidden"
    assert _org_policy(a.id) == "synthetic_only"


def test_schema_only_requires_acknowledged_consent(two_orgs):
    a = two_orgs.a
    _require_route(_consents_url(a))
    resp = api_client_for(a.owner).post(_consents_url(a), _consent_body(acknowledged=False), format="json")
    assert 400 <= resp.status_code < 500
    assert _code(resp) in ("ai_policy_consent_required", "invalid")
    assert _org_policy(a.id) == "synthetic_only"


def test_stale_consent_text_version_is_refused(two_orgs):
    a = two_orgs.a
    _require_route(_consents_url(a))
    resp = api_client_for(a.owner).post(
        _consents_url(a), _consent_body(consent_text_version="1999-01-v0"), format="json"
    )
    assert 400 <= resp.status_code < 500
    assert _org_policy(a.id) == "synthetic_only"


@pytest.mark.parametrize("role", ["analyst", "viewer", "data_engineer", "auditor"])
def test_non_admins_cannot_consent(two_orgs, role):
    a = two_orgs.a
    _require_route(_consents_url(a))
    resp = api_client_for(a.user(role)).post(_consents_url(a), _consent_body(), format="json")
    assert resp.status_code == 403
    assert _org_policy(a.id) == "synthetic_only"


def test_consent_requires_reauthentication(two_orgs):
    a = two_orgs.a
    _require_route(_consents_url(a))
    resp = api_client_for(a.owner).post(_consents_url(a), _consent_body(password="wrong-password-123"), format="json")
    assert resp.status_code in (400, 401, 403)
    assert _org_policy(a.id) == "synthetic_only"


def test_owner_consent_then_revoke_is_immediate(two_orgs):
    a = two_orgs.a
    _require_route(_consents_url(a))
    client = api_client_for(a.owner)
    assert client.post(_consents_url(a), _consent_body(), format="json").status_code == 201
    assert _org_policy(a.id) == "schema_only"
    policy = client.get(f"/api/v1/orgs/{a.slug}/ai-policy").json()
    assert policy["policy"] == "schema_only" and policy["consent"]["valid"] is True
    assert client.post(f"/api/v1/orgs/{a.slug}/ai-policy/revoke", {}, format="json").status_code == 200
    assert _org_policy(a.id) == "synthetic_only"
    # cross-org: B is untouched
    assert _org_policy(two_orgs.b.id) == "synthetic_only"


def test_consent_in_one_org_does_not_affect_another(two_orgs):
    a, b = two_orgs
    _require_route(_consents_url(a))
    api_client_for(a.owner).post(_consents_url(a), _consent_body(), format="json")
    assert _org_policy(b.id) == "synthetic_only"
    assert api_client_for(a.owner).post(_consents_url(b), _consent_body(), format="json").status_code == 404


# ----------------------------------------------------------------------------------- AI gate (apps.ai)
def _gate():
    try:
        from apps.ai import services
    except ImportError:
        return None
    return getattr(services, "check_policy", None)


def _call_gate(gate, org_id, payload):
    params = list(inspect.signature(gate).parameters)
    with tenant_context(org_id):
        return gate(org_id, payload) if len(params) >= 2 else gate(payload)


@pytest.fixture
def synthetic_asset_kind():
    from apps.core import registry

    registry.register_asset_classifier("qa_synthetic", lambda _id: "synthetic")
    registry.register_asset_classifier("qa_internal", lambda _id: "internal")
    yield
    registry._classifiers.pop("qa_synthetic", None)
    registry._classifiers.pop("qa_internal", None)


@pytest.mark.parametrize(
    ("policy", "payload_class", "kind", "allowed"),
    [
        ("synthetic_only", "VALUES", "qa_internal", False),
        ("synthetic_only", "SCHEMA", "qa_internal", False),
        ("synthetic_only", "DOCUMENT_TEXT", "qa_internal", False),
        ("synthetic_only", "VALUES", "qa_synthetic", True),
        ("synthetic_only", "SCHEMA", "qa_synthetic", True),
        ("full", "VALUES", "qa_internal", False),  # manual DB edit + free tier: still blocked
        ("aggregates_only", "VALUES", "qa_internal", False),
        ("schema_only", "SCHEMA", "qa_internal", False),  # no consent recorded
    ],
)
def test_policy_gate(two_orgs, settings, synthetic_asset_kind, network_spy, policy, payload_class, kind, allowed):
    from apps.core.exceptions import AIPolicyBlocked
    from nexora.contracts import PayloadClass, PayloadManifest

    gate = _gate()
    if gate is None:
        pytest.skip("apps.ai.services.check_policy not implemented yet (AI/ML agent)")
    settings.GEMINI_TIER = "free"
    a = two_orgs.a
    set_org_fields(a.org, ai_data_policy=policy)
    payload = PayloadManifest(items={PayloadClass(payload_class): (f"{kind}:{uuid.uuid4()}",)})
    try:
        if allowed:
            _call_gate(gate, a.id, payload)
        else:
            with pytest.raises(AIPolicyBlocked):
                _call_gate(gate, a.id, payload)
    finally:
        clear_db_tenant()
    assert network_spy.external == []


def test_unknown_assets_are_treated_as_confidential():
    from apps.core.registry import classify_asset

    assert classify_asset(f"nonexistent_kind:{uuid.uuid4()}") == "confidential"
    assert classify_asset("garbage") == "confidential"


def test_blocked_gemini_call_makes_no_network_request(two_orgs, synthetic_asset_kind, network_spy, settings):
    try:
        from apps.ai import services
    except ImportError:
        services = None
    gemini = getattr(services, "gemini", None)
    if gemini is None:
        pytest.skip("apps.ai.services.gemini not implemented yet (AI/ML agent)")
    from apps.core.exceptions import AIPolicyBlocked, NexoraError
    from nexora.contracts import PayloadClass, PayloadManifest

    settings.GEMINI_API_KEY = "AIza-synthetic-test-key-never-valid"
    a = two_orgs.a
    payload = PayloadManifest(items={PayloadClass.VALUES: (f"qa_internal:{uuid.uuid4()}",)})
    with tenant_context(a.id):
        with pytest.raises((AIPolicyBlocked, NexoraError)) as info:
            gemini().generate_structured(
                feature="qa.policy", prompt_key="qa.policy.probe", system="s", prompt="p", schema={}, payload=payload
            )
    clear_db_tenant()
    assert getattr(info.value, "default_code", "") == "ai_policy_blocked" or "policy" in str(info.value).lower()
    assert network_spy.external == []
