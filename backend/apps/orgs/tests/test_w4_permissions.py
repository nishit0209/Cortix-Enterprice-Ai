"""W4 permissions (CONTRACT "W4 additions / Permissions"): ML, Customer 360 and Product 360 grants in the system
roles, the idempotent data migration orgs/0004 for existing orgs, and the `ml.predict` API-key scope."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps
from rest_framework.test import APIClient

from apps.accounts import services as accounts
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, client_for, member_of, member_with_permissions, org_url
from apps.orgs.permissions_catalog import PERMISSIONS, SYSTEM_ROLES

mig = importlib.import_module("apps.orgs.migrations.0004_w4_permissions_and_org_status")

W4 = {"ml.view", "ml.manage", "ml.promote", "ml.predict", "customers.view", "customers.manage", "customers.review",
      "products.view", "products.manage", "products.review"}


# ================================================================================================ unit (no DB)
def test_every_w4_permission_is_in_the_catalog_and_the_migration():
    assert W4 <= set(PERMISSIONS)
    assert set(mig.W4_PERMISSIONS) == W4
    assert all(mig.W4_PERMISSIONS[c] == PERMISSIONS[c] for c in W4)


@pytest.mark.parametrize("role", sorted(SYSTEM_ROLES))
def test_the_frozen_migration_grants_match_the_live_system_roles(role):
    assert set(SYSTEM_ROLES[role]["permissions"]) & W4 == set(mig.W4_GRANTS.get(role, set()))


def test_contract_grants_per_role():
    perms = {k: set(v["permissions"]) for k, v in SYSTEM_ROLES.items()}
    assert W4 <= perms["owner"] and W4 <= perms["admin"]
    work = {"ml.view", "ml.manage", "customers.view", "customers.manage", "customers.review", "products.view",
            "products.manage", "products.review"}
    for role in ("analyst", "data_engineer", "data_scientist"):
        assert work <= perms[role], role
        assert "ml.promote" not in perms[role], role  # ml.promote: Admin/Owner only
    assert perms["viewer"] & W4 == {"ml.view", "customers.view", "products.view"}
    assert perms["auditor"] & W4 == {"ml.view", "customers.view", "products.view"}
    assert "ml.predict" in perms["data_scientist"] and "ml.predict" not in perms["analyst"]


def test_ml_predict_is_a_grantable_api_key_scope():
    assert "ml.predict" in accounts.API_KEY_ALLOWED_SCOPES
    assert accounts.API_KEY_FORBIDDEN_SCOPES & W4 == {"ml.promote"}  # promotion is interactive-only


class _Cursor:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchone(self):
        return ("nexora_owner",)

    def fetchall(self):
        return self.rows


def test_org_listing_switches_to_the_definer_role_and_back():
    cur = _Cursor([("o1",), ("o2",)])
    assert mig._org_ids(cur) == ["o1", "o2"]
    assert cur.sql[1] == "SET LOCAL ROLE nexora_definer" and cur.sql[-1] == 'SET LOCAL ROLE "nexora_owner"'


# ================================================================================================ DB
def _role_perms(org, key) -> set[str]:
    from apps.orgs.models import Role, RolePermission

    with tenant_context(org.id):
        role = Role.objects.get(key=key)
        return set(RolePermission.objects.filter(role=role).values_list("permission__codename", flat=True))


@pytest.mark.django_db
def test_a_new_org_gets_the_w4_grants(org):
    for key, spec in SYSTEM_ROLES.items():
        assert _role_perms(org, key) == set(spec["permissions"]), key


@pytest.mark.django_db
def test_the_data_migration_backfills_existing_orgs_idempotently(org, other_org):
    from apps.orgs.models import Permission, RolePermission

    with tenant_context(org.id):  # an org created before W4: no W4 grants at all
        RolePermission.objects.filter(permission__codename__in=W4).delete()
    assert not _role_perms(org, "analyst") & W4
    perms = mig.ensure_catalog(Permission)
    assert set(perms) == W4
    with tenant_context(org.id):
        added = mig.grant_for_current_org(django_apps, perms)
        assert added == sum(len(g) for g in mig.W4_GRANTS.values())
        assert mig.grant_for_current_org(django_apps, perms) == 0  # idempotent
    for key in mig.W4_GRANTS:
        assert _role_perms(org, key) & W4 == set(mig.W4_GRANTS[key]), key
    assert _role_perms(other_org, "viewer") & W4 == {"ml.view", "customers.view", "products.view"}  # untouched


@pytest.mark.django_db
def test_the_migration_never_touches_custom_roles(org):
    from apps.orgs.models import Permission

    member_with_permissions(org, {"org.view"}, key="custom_reader")
    with tenant_context(org.id):
        mig.grant_for_current_org(django_apps, mig.ensure_catalog(Permission))
    assert _role_perms(org, "custom_reader") == {"org.view"}


@pytest.mark.django_db
def test_org_payload_lists_the_w4_permissions_per_role(org, owner_client):
    assert W4 <= set(owner_client.get(org_url(org)).json()["permissions"])
    viewer = client_for(member_of(org, "viewer"))
    assert set(viewer.get(org_url(org)).json()["permissions"]) & W4 == {"ml.view", "customers.view", "products.view"}


# ------------------------------------------------------------------------------------------------ ml.predict keys
def _create_key(client, org, scopes):
    return client.post(org_url(org, "api-keys"), {"name": "predict", "scopes": scopes, "password": DEFAULT_PASSWORD},
                       format="json")


@pytest.mark.django_db
def test_an_ml_predict_key_carries_only_that_scope(org, owner_client):
    r = _create_key(owner_client, org, ["ml.predict"])
    assert r.status_code == 201, r.content
    bearer = APIClient()
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {r.json()['secret']}")
    from rest_framework.test import APIRequestFactory

    from apps.accounts.authentication import ApiKeyAuthentication

    request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {r.json()['secret']}")
    ApiKeyAuthentication().authenticate(request)
    assert request._nexora_api_key_tenant.permissions == frozenset({"ml.predict"})
    # a predict-only key cannot read the org (org.view is not in its scopes)
    assert bearer.get(org_url(org, "members")).status_code in (403, 404)


@pytest.mark.django_db
def test_ml_predict_needs_the_creator_to_hold_it(org):
    holder = member_with_permissions(org, {"org.view", "api_keys.manage", "ml.view", "ml.predict"})
    r = _create_key(client_for(holder), org, ["ml.predict"])
    assert r.status_code == 201, r.content
    lacking = member_with_permissions(org, {"org.view", "api_keys.manage", "ml.view"})
    r = _create_key(client_for(lacking), org, ["ml.predict"])
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "scopes"


@pytest.mark.django_db
def test_an_ml_predict_key_loses_the_scope_when_the_creator_loses_it(org):
    from apps.orgs.models import RolePermission

    holder = member_with_permissions(org, {"org.view", "api_keys.manage", "ml.predict"}, key="predictors")
    secret = _create_key(client_for(holder), org, ["ml.predict"]).json()["secret"]
    with tenant_context(org.id):
        RolePermission.objects.filter(role__key="predictors", permission__codename="ml.predict").delete()
    from rest_framework.test import APIRequestFactory

    from apps.accounts.authentication import ApiKeyAuthentication

    request = APIRequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {secret}")
    ApiKeyAuthentication().authenticate(request)
    assert request._nexora_api_key_tenant.permissions == frozenset()
