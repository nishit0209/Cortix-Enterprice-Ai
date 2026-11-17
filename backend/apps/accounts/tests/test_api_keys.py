"""API-key management endpoints and ApiKeyAuthentication. Runs as nexora_app (RLS enforced)."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ApiKey
from apps.accounts.services import create_api_key, hash_api_secret, parse_api_key
from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url
from apps.orgs.models import Membership, Permission, Role, RolePermission

pytestmark = pytest.mark.django_db


def bearer(token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return c


def new_key(org, user, scopes=("org.view",), **kw):
    with tenant_context(org.id):
        return create_api_key(name="synthetic-ci", scopes=list(scopes), created_by=user, **kw)


# --- management API ---------------------------------------------------------------------------------------
def test_create_returns_secret_once_and_stores_only_hmac(org, owner_client):
    r = owner_client.post(org_url(org, "api-keys"), {"name": "CI pipeline", "scopes": ["org.view", "ml.predict"]},
                          format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    secret = body["secret"]
    prefix, raw_secret = parse_api_key(secret)
    assert prefix == body["prefix"] and len(prefix) == 8
    assert body["scopes"] == ["ml.predict", "org.view"]
    assert body["status"] == "active"
    with tenant_context(org.id):
        key = ApiKey.objects.get(id=body["id"])
        assert bytes(key.key_hash) == hash_api_secret(raw_secret)
        assert raw_secret not in str(bytes(key.key_hash))
        assert AuditEvent.objects.filter(action="api_key.create", target_id=str(key.id)).exists()
        ev = AuditEvent.objects.get(action="api_key.create")
        assert "secret" not in str(ev.metadata)
    listed = owner_client.get(org_url(org, "api-keys")).json()
    assert [k["id"] for k in listed["items"]] == [body["id"]]
    assert "secret" not in listed["items"][0]
    assert listed["next_cursor"] is None


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"name": "x", "scopes": []}, "scopes"),
        ({"name": "x", "scopes": ["members.manage"]}, "scopes"),
        ({"name": "x", "scopes": ["api_keys.manage"]}, "scopes"),
        ({"name": "x", "scopes": ["no.such.scope"]}, "scopes"),
        ({"scopes": ["org.view"]}, "name"),
        ({"name": "x", "scopes": ["org.view"], "expires_at": "2001-01-01T00:00:00Z"}, "expires_at"),
    ],
)
def test_create_validation(org, owner_client, payload, field):
    r = owner_client.post(org_url(org, "api-keys"), payload, format="json")
    assert r.status_code == 400
    assert any(e["field"] == field for e in r.json()["errors"])


def test_cannot_grant_scopes_beyond_own_permissions(org):
    with tenant_context(org.id):
        role = Role.objects.create(key="key_manager", name="Key manager")
        for code in ("api_keys.manage", "org.view"):
            RolePermission.objects.create(role=role, permission=Permission.objects.get(codename=code))
    user = member_of(org, "viewer")
    with tenant_context(org.id):
        Membership.objects.filter(user=user).update(role=role)
    client = client_for(user)
    assert client.post(org_url(org, "api-keys"), {"name": "ok", "scopes": ["org.view"]}, format="json").status_code == 201
    r = client.post(org_url(org, "api-keys"), {"name": "bad", "scopes": ["ml.manage"]}, format="json")
    assert r.status_code == 400


def test_permissions_and_visibility(org, other_org, owner_client):
    analyst = client_for(member_of(org, "analyst"))
    assert analyst.get(org_url(org, "api-keys")).status_code == 403
    assert analyst.post(org_url(org, "api-keys"), {"name": "x", "scopes": ["org.view"]}, format="json").status_code == 403
    assert owner_client.get(org_url(other_org, "api-keys")).status_code == 404
    assert APIClient().get(org_url(org, "api-keys")).status_code == 401


def test_revoke_and_cross_org_404(org, other_org, owner, owner_client):
    key, token = new_key(org, owner)
    with tenant_context(other_org.id):
        foreign_owner = Membership.objects.get().user
    foreign_key, _ = new_key(other_org, foreign_owner)
    assert owner_client.delete(org_url(org, f"api-keys/{foreign_key.id}")).status_code == 404
    assert owner_client.delete(org_url(org, f"api-keys/{key.id}")).status_code == 204
    assert owner_client.delete(org_url(org, f"api-keys/{key.id}")).status_code == 204  # idempotent
    with tenant_context(org.id):
        key.refresh_from_db()
        assert key.revoked_at is not None
        assert AuditEvent.objects.filter(action="api_key.revoke").count() == 1
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 401


# --- authentication -----------------------------------------------------------------------------------------
def test_api_key_authenticates_as_creator_within_scopes(org, owner):
    key, token = new_key(org, owner, scopes=("org.view",))
    client = bearer(token)
    r = client.get(org_url(org, "ai-policy"))
    assert r.status_code == 200
    assert client.get(org_url(org)).json()["role"] == "api_key"
    assert client.get(org_url(org)).json()["permissions"] == ["org.view"]
    with tenant_context(org.id):
        key.refresh_from_db()
        assert key.last_used_at is not None


def test_api_key_missing_scope_is_403(org, owner):
    _, token = new_key(org, owner, scopes=("datasets.view",))
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 403


def test_api_key_is_bound_to_its_org(org, other_org, owner):
    _, token = new_key(org, owner)
    assert bearer(token).get(org_url(other_org, "ai-policy")).status_code == 404
    assert bearer(token).get("/api/v1/orgs/does-not-exist/ai-policy").status_code == 404


def test_api_key_cannot_reach_management_endpoints(org, owner):
    _, token = new_key(org, owner, scopes=("org.view", "members.view", "roles.view"))
    client = bearer(token)
    assert client.get(org_url(org, "members")).status_code == 403
    assert client.get(org_url(org, "roles")).status_code == 403
    assert client.get(org_url(org, "api-keys")).status_code == 403
    r = client.post(org_url(org, "ai-policy/revoke"))
    assert r.status_code == 403
    assert client.post("/api/v1/orgs", {"name": "Evil"}, format="json").status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Bearer nxk_aaaaaaaa_" + "x" * 43,         # unknown prefix
        "Bearer nxk_bad",                          # malformed
        "Bearer nxk_aaaaaaaa_short",               # malformed secret
        "Bearer a b",                              # too many parts
    ],
)
def test_invalid_keys_are_401(org, header):
    r = APIClient().get(org_url(org, "ai-policy"), HTTP_AUTHORIZATION=header)
    assert r.status_code == 401
    assert r["WWW-Authenticate"].startswith("Bearer")


def test_wrong_secret_is_401(org, owner):
    key, token = new_key(org, owner)
    prefix, secret = parse_api_key(token)
    tampered = f"nxk_{prefix}_{'A' * len(secret)}"
    assert bearer(tampered).get(org_url(org, "ai-policy")).status_code == 401


def test_expired_key_is_401(org, owner):
    key, token = new_key(org, owner, expires_at=timezone.now() + timedelta(days=1))
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 200
    with tenant_context(org.id):
        ApiKey.objects.filter(id=key.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 401


def test_key_dies_when_creator_leaves_or_is_downgraded(org):
    admin = member_of(org, "admin")
    _, token = new_key(org, admin, scopes=("org.view", "audit.view"))
    assert bearer(token).get(org_url(org, "audit-events")).status_code == 200
    with tenant_context(org.id):
        Membership.objects.filter(user=admin).update(role=Role.objects.get(key="viewer"))
    assert bearer(token).get(org_url(org, "audit-events")).status_code == 403  # scope no longer held
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 200
    with tenant_context(org.id):
        Membership.objects.filter(user=admin).delete()
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 401


def test_key_for_inactive_creator_is_401(org, owner):
    _, token = new_key(org, owner)
    owner.is_active = False
    owner.save()
    assert bearer(token).get(org_url(org, "ai-policy")).status_code == 401


def test_non_nexora_bearer_falls_through_to_session(org, owner):
    client = client_for(owner)
    client.credentials(HTTP_AUTHORIZATION="Bearer some-other-token")
    assert client.get(org_url(org, "ai-policy")).status_code == 200
