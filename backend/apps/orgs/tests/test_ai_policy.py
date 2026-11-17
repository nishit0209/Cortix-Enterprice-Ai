"""AI data policy (security critical): consent ledger, re-auth, tier rules, ai_policy_state() contract."""

import pytest
from django.db import DatabaseError, transaction

from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, client_for, member_of, org_url
from apps.orgs import services
from apps.orgs.models import AIPolicyConsent, Organization

pytestmark = pytest.mark.django_db

VERSION = "2026-11-v1"


@pytest.fixture(autouse=True)
def _policy_settings(settings):
    settings.AI_CONSENT_TEXT_VERSION = VERSION
    settings.GEMINI_TIER = "free"
    return settings


def consent_body(**over):
    return {"policy": "schema_only", "consent_text_version": VERSION, "acknowledged": True,
            "password": DEFAULT_PASSWORD, **over}


def grant(client, org, **over):
    return client.post(org_url(org, "ai-policy/consents"), consent_body(**over), format="json")


def state(org):
    with tenant_context(org.id):
        return services.ai_policy_state()


# --- reading -----------------------------------------------------------------------------------------------------
def test_default_policy_payload(org, owner_client):
    r = owner_client.get(org_url(org, "ai-policy"))
    assert r.status_code == 200
    assert r.json() == {
        "policy": "synthetic_only",
        "configured_policy": "synthetic_only",
        "gemini_tier": "free",
        "consent": None,
        "allowed_values": ["synthetic_only", "schema_only"],
        "consent_text_version": VERSION,
    }
    assert state(org) == {"policy": "synthetic_only", "consent_valid": True}


def test_allowed_values_on_paid_tier(org, owner_client, settings):
    settings.GEMINI_TIER = "paid"
    assert owner_client.get(org_url(org, "ai-policy")).json()["allowed_values"] == [
        "synthetic_only", "schema_only", "aggregates_only", "full"]


def test_consent_text(org, owner_client):
    r = owner_client.get(org_url(org, "ai-policy/consent-text"))
    assert r.status_code == 200
    assert r.json()["version"] == VERSION
    assert VERSION in r.json()["text"] and "Gemini" in r.json()["text"]


def test_read_endpoints_visibility(org, other_org, owner_client, anon_client):
    viewer = client_for(member_of(org, "viewer"))
    assert viewer.get(org_url(org, "ai-policy")).status_code == 200
    assert viewer.get(org_url(org, "ai-policy/consent-text")).status_code == 200
    assert owner_client.get(org_url(other_org, "ai-policy")).status_code == 404
    assert anon_client.get(org_url(org, "ai-policy")).status_code == 401


# --- granting ------------------------------------------------------------------------------------------------------
@pytest.mark.ac("P2-AC4")
def test_grant_schema_only(org, owner, owner_client):
    r = owner_client.post(org_url(org, "ai-policy/consents"), consent_body(), format="json",
                          HTTP_USER_AGENT="synthetic-agent/1.0")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["policy"] == "schema_only" and body["configured_policy"] == "schema_only"
    assert body["consent"]["valid"] is True
    assert body["consent"]["version"] == VERSION
    assert body["consent"]["granted_by"] == owner.email
    assert body["consent"]["granted_at"]
    assert state(org) == {"policy": "schema_only", "consent_valid": True}
    assert owner_client.get(org_url(org)).json()["ai_data_policy"] == "schema_only"
    with tenant_context(org.id):
        row = AIPolicyConsent.objects.get()
        assert (row.action, row.policy, row.consent_text_version, row.gemini_tier) == ("grant", "schema_only",
                                                                                       VERSION, "free")
        assert row.actor_id == owner.id and row.ip == "127.0.0.1"
        assert row.user_agent == "synthetic-agent/1.0"
        ev = AuditEvent.objects.get(action="ai.consent.grant")
        assert ev.changes == {"ai_data_policy": ["synthetic_only", "schema_only"]}
        assert "password" not in str(ev.metadata)


def test_grant_requires_correct_password(org, owner_client):
    r = grant(owner_client, org, password="Not-the-password-1")
    assert r.status_code == 400
    assert r.json()["code"] == "reauth_failed"
    assert r.json()["errors"][0]["field"] == "password"
    assert state(org)["policy"] == "synthetic_only"
    with tenant_context(org.id):
        assert not AIPolicyConsent.objects.exists()


@pytest.mark.parametrize("over", [{"acknowledged": False}, {"consent_text_version": "2020-01-v0"}])
def test_grant_requires_acknowledged_current_text(org, owner_client, over):
    r = grant(owner_client, org, **over)
    assert r.status_code == 400
    assert r.json()["code"] == "ai_policy_consent_required"
    assert state(org)["policy"] == "synthetic_only"


@pytest.mark.parametrize("policy", ["aggregates_only", "full"])
def test_paid_only_policies_refused_on_free_tier(org, owner_client, policy):
    r = grant(owner_client, org, policy=policy)
    assert r.status_code == 403
    assert r.json()["code"] == "ai_policy_tier_forbidden"
    assert state(org)["policy"] == "synthetic_only"


def test_paid_tier_allows_full_then_tier_downgrade_invalidates(org, owner_client, settings):
    settings.GEMINI_TIER = "paid"
    r = grant(owner_client, org, policy="full")
    assert r.status_code == 201 and r.json()["policy"] == "full"
    assert state(org) == {"policy": "full", "consent_valid": True}
    settings.GEMINI_TIER = "free"
    assert state(org) == {"policy": "full", "consent_valid": False}
    body = owner_client.get(org_url(org, "ai-policy")).json()
    assert body["policy"] == "synthetic_only" and body["configured_policy"] == "full"
    assert body["consent"]["valid"] is False


def test_switching_policy_revokes_previous(org, owner_client, settings):
    settings.GEMINI_TIER = "paid"
    grant(owner_client, org, policy="full")
    grant(owner_client, org, policy="schema_only")
    with tenant_context(org.id):
        rows = list(AIPolicyConsent.objects.order_by("created_at", "id").values_list("action", "policy"))
    assert rows == [("grant", "full"), ("revoke", "full"), ("grant", "schema_only")]
    assert state(org) == {"policy": "schema_only", "consent_valid": True}


def test_invalid_body(org, owner_client):
    r = owner_client.post(org_url(org, "ai-policy/consents"), {"policy": "synthetic_only"}, format="json")
    assert r.status_code == 400
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"policy", "consent_text_version", "acknowledged", "password"} <= fields


def test_grant_permissions(org, other_org, owner_client):
    analyst = client_for(member_of(org, "analyst"))
    assert grant(analyst, org).status_code == 403
    assert grant(owner_client, other_org).status_code == 404
    admin = client_for(member_of(org, "admin"))
    assert grant(admin, org).status_code == 201  # admins can consent too


# --- version bump / revoke ------------------------------------------------------------------------------------------
def test_consent_text_version_bump_invalidates_consent(org, owner_client, settings):
    grant(owner_client, org)
    settings.AI_CONSENT_TEXT_VERSION = "2027-01-v2"
    assert state(org) == {"policy": "schema_only", "consent_valid": False}
    body = owner_client.get(org_url(org, "ai-policy")).json()
    assert body["policy"] == "synthetic_only"
    assert body["consent"] == {"valid": False, "version": VERSION, "granted_by": body["consent"]["granted_by"],
                               "granted_at": body["consent"]["granted_at"]}
    assert owner_client.get(org_url(org)).json()["ai_data_policy"] == "synthetic_only"
    r = grant(owner_client, org, consent_text_version="2027-01-v2")
    assert r.status_code == 201 and r.json()["consent"]["valid"] is True


def test_revoke(org, owner_client):
    grant(owner_client, org)
    r = owner_client.post(org_url(org, "ai-policy/revoke"))
    assert r.status_code == 200
    assert r.json()["policy"] == "synthetic_only" and r.json()["consent"] is None
    assert state(org) == {"policy": "synthetic_only", "consent_valid": True}
    with tenant_context(org.id):
        assert list(AIPolicyConsent.objects.order_by("created_at", "id").values_list("action", flat=True)) == [
            "grant", "revoke"]
        assert AuditEvent.objects.filter(action="ai.consent.revoke").count() == 1
        assert Organization.objects.get().ai_data_policy == "synthetic_only"
    assert owner_client.post(org_url(org, "ai-policy/revoke")).status_code == 200  # idempotent
    with tenant_context(org.id):
        assert AIPolicyConsent.objects.count() == 2


def test_revoke_permissions(org, other_org, owner_client):
    assert client_for(member_of(org, "viewer")).post(org_url(org, "ai-policy/revoke")).status_code == 403
    assert owner_client.post(org_url(other_org, "ai-policy/revoke")).status_code == 404


def test_latest_revoke_record_invalidates_even_if_org_row_not_updated(org, owner, owner_client):
    """Defence in depth: the ledger, not the org column, decides validity."""
    grant(owner_client, org)
    with tenant_context(org.id):
        services._consent_row("revoke", "schema_only", VERSION, owner.pk)
    assert state(org) == {"policy": "schema_only", "consent_valid": False}


def test_consent_ledger_is_append_only(org, owner_client):
    grant(owner_client, org)
    with tenant_context(org.id):
        row = AIPolicyConsent.objects.get()
        with pytest.raises(DatabaseError), transaction.atomic():
            AIPolicyConsent.objects.filter(id=row.id).update(consent_text_version="forged")
        with pytest.raises(DatabaseError), transaction.atomic():
            AIPolicyConsent.objects.filter(id=row.id).delete()


def test_consents_are_tenant_isolated(org, other_org, owner_client):
    grant(owner_client, org)
    with tenant_context(other_org.id):
        assert not AIPolicyConsent.objects.exists()
        assert services.ai_policy_state() == {"policy": "synthetic_only", "consent_valid": True}
