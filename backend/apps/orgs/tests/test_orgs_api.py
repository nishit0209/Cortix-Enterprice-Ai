"""POST /orgs, GET/PATCH /orgs/{org}, roles, permissions catalog, activity. RLS enforced (nexora_app)."""

import pytest

from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url
from apps.orgs.models import Organization
from apps.orgs.permissions_catalog import PERMISSIONS, SYSTEM_ROLES

pytestmark = pytest.mark.django_db

ORG_KEYS = {"id", "slug", "name", "plan", "status", "region", "ai_data_policy", "created_at", "role", "permissions"}


# --- POST /orgs ----------------------------------------------------------------------------------------------
def test_create_org_caller_becomes_owner(owner, owner_client):
    r = owner_client.post("/api/v1/orgs", {"name": "Gamma Synthetic"}, format="json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert ORG_KEYS <= set(body)
    assert body["role"] == "owner"
    assert body["ai_data_policy"] == "synthetic_only"
    assert body["permissions"] == sorted(PERMISSIONS)
    slugs = [o["slug"] for o in owner_client.get("/api/v1/me").json()["orgs"]]
    assert body["slug"] in slugs
    listed = owner_client.get("/api/v1/orgs").json()["items"]
    assert body["slug"] in [o["slug"] for o in listed]


def test_create_org_validation_and_auth(owner_client, anon_client):
    assert owner_client.post("/api/v1/orgs", {"name": "x"}, format="json").status_code == 400
    assert owner_client.post("/api/v1/orgs", {}, format="json").status_code == 400
    assert anon_client.post("/api/v1/orgs", {"name": "Anon Org"}, format="json").status_code == 401


def test_create_org_ignores_policy_in_body(owner_client):
    r = owner_client.post("/api/v1/orgs", {"name": "Delta Synthetic", "ai_data_policy": "full"}, format="json")
    assert r.status_code == 201
    assert r.json()["ai_data_policy"] == "synthetic_only"


def test_duplicate_names_get_unique_slugs(owner_client):
    a = owner_client.post("/api/v1/orgs", {"name": "Same Name"}, format="json").json()["slug"]
    b = owner_client.post("/api/v1/orgs", {"name": "Same Name"}, format="json").json()["slug"]
    assert a != b and b.startswith("same-name")


# --- GET/PATCH /orgs/{org} -------------------------------------------------------------------------------------
def test_get_org_shape_and_role_permissions(org, owner_client):
    r = owner_client.get(org_url(org))
    assert r.status_code == 200
    body = r.json()
    assert ORG_KEYS <= set(body)
    assert body["id"] == str(org.id) and body["slug"] == org.slug and body["plan"] == "free"
    viewer = client_for(member_of(org, "viewer")).get(org_url(org)).json()
    assert viewer["role"] == "viewer"
    assert viewer["permissions"] == sorted(SYSTEM_ROLES["viewer"]["permissions"])


def test_get_org_visibility(org, other_org, owner_client, anon_client):
    assert owner_client.get(org_url(other_org)).status_code == 404
    assert owner_client.get("/api/v1/orgs/no-such-org").status_code == 404
    assert anon_client.get(org_url(org)).status_code == 401
    assert anon_client.get(org_url(other_org, "members")).status_code == 401


def test_suspended_member_cannot_see_org(org):
    user = member_of(org, "analyst")
    from apps.orgs.models import Membership

    with tenant_context(org.id):
        Membership.objects.filter(user=user).update(status="suspended")
    assert client_for(user).get(org_url(org)).status_code == 404


def test_patch_org(org, owner_client):
    r = owner_client.patch(org_url(org), {"name": "Acme Renamed", "settings": {"theme": "dark", "row_cap": 500}},
                           format="json")
    assert r.status_code == 200, r.content
    assert r.json()["name"] == "Acme Renamed"
    assert r.json()["settings"] == {"theme": "dark", "row_cap": 500}
    r = owner_client.patch(org_url(org), {"settings": {"theme": None, "lang": "en"}}, format="json")
    assert r.json()["settings"] == {"row_cap": 500, "lang": "en"}
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="org.update").count() == 2


def test_patch_org_cannot_change_ai_policy(org, owner_client):
    r = owner_client.patch(org_url(org), {"ai_data_policy": "full"}, format="json")
    assert r.status_code == 400
    assert r.json()["errors"][0]["field"] == "ai_data_policy"
    with tenant_context(org.id):
        assert Organization.objects.get().ai_data_policy == "synthetic_only"


def test_patch_org_permissions_and_validation(org, other_org, owner_client):
    analyst = client_for(member_of(org, "analyst"))
    assert analyst.patch(org_url(org), {"name": "Hijack"}, format="json").status_code == 403
    assert owner_client.patch(org_url(other_org), {"name": "Hijack"}, format="json").status_code == 404
    assert owner_client.patch(org_url(org), {"name": "x"}, format="json").status_code == 400
    assert owner_client.patch(org_url(org), {"settings": "not-a-dict"}, format="json").status_code == 400
    big = {"blob": "x" * 20000}
    assert owner_client.patch(org_url(org), {"settings": big}, format="json").status_code == 400


# --- roles / permissions ----------------------------------------------------------------------------------------
def test_roles_list(org, owner_client):
    r = owner_client.get(org_url(org, "roles"))
    assert r.status_code == 200
    roles = {x["key"]: x for x in r.json()}
    assert set(roles) == set(SYSTEM_ROLES)
    assert roles["analyst"]["permissions"] == sorted(SYSTEM_ROLES["analyst"]["permissions"])
    assert all(x["is_system"] for x in roles.values())
    assert {"id", "key", "name", "is_system", "permissions"} <= set(roles["owner"])


def test_roles_permissions_and_visibility(org, other_org, owner_client):
    viewer = client_for(member_of(org, "viewer"))
    assert viewer.get(org_url(org, "roles")).status_code == 403
    assert owner_client.get(org_url(other_org, "roles")).status_code == 404


def test_permission_catalog(owner_client, anon_client):
    r = owner_client.get("/api/v1/permissions")
    assert r.status_code == 200
    assert {p["codename"] for p in r.json()} == set(PERMISSIONS)
    assert anon_client.get("/api/v1/permissions").status_code == 401


# --- activity feed -----------------------------------------------------------------------------------------------
def test_activity_feed_has_actor_email(org, owner, owner_client, other_org):
    owner_client.patch(org_url(org), {"name": "Acme Activity"}, format="json")
    r = owner_client.get(org_url(org, "activity"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["action"] for i in items][:2] == ["org.update", "org.create"]
    assert items[0]["actor_email"] == owner.email
    assert items[0]["actor_name"] == owner.full_name
    assert items[0]["actor_type"] == "user"
    assert all(i["action"] != "org.create" or i["target_id"] == str(org.id) for i in items)  # no other org


def test_activity_feed_is_limited_and_member_only(org, owner_client, other_org):
    with tenant_context(org.id):
        from apps.core.audit import record

        for i in range(60):
            record("synthetic.event", metadata={"i": i})
    assert len(owner_client.get(org_url(org, "activity")).json()["items"]) == 50
    viewer = client_for(member_of(org, "viewer"))
    assert viewer.get(org_url(org, "activity")).status_code == 200
    assert owner_client.get(org_url(other_org, "activity")).status_code == 404
