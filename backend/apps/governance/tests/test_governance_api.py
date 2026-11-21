"""Governance endpoints + DB-backed effective_policy. Runs as nexora_app (RLS enforced)."""

from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from apps.accounts.services import create_api_key
from apps.core import registry
from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, make_user, member_of, org_url
from apps.governance import services
from apps.governance.models import ColumnPolicy, DatasetAcl, RowPolicy, UserAttribute
from apps.orgs.models import Membership, Role

pytestmark = pytest.mark.django_db

TABLES = {"sales": "org_synth.ds_1_v1"}
COLUMNS = {"sales": ["order_id", "region", "amount", "email"]}


@pytest.fixture
def ds(monkeypatch):
    """A synthetic dataset id. These API tests exercise governance on its own, so the dataset-existence check is
    stubbed (any id exists) for the test only; the real datasets checker is restored afterwards."""
    monkeypatch.setitem(registry._exists, "dataset", lambda asset_id: True)
    return uuid4()


def role_id(org, key) -> str:
    with tenant_context(org.id):
        return str(Role.objects.get(key=key).id)


def url(org, ds, path):
    return org_url(org, f"datasets/{ds}/{path}")


# --- ACL ------------------------------------------------------------------------------------------------------------
def test_acl_crud(org, owner_client, ds):
    analyst = member_of(org, "analyst")
    r = owner_client.post(url(org, ds, "acl"), {"principal_type": "user", "principal_id": str(analyst.id),
                                                "privilege": "query"}, format="json")
    assert r.status_code == 201, r.content
    acl = r.json()
    assert acl["dataset_id"] == str(ds) and acl["privilege"] == "query"
    dup = owner_client.post(url(org, ds, "acl"), {"principal_type": "user", "principal_id": str(analyst.id),
                                                  "privilege": "query"}, format="json")
    assert dup.status_code == 409
    r = owner_client.post(url(org, ds, "acl"), {"principal_type": "role", "principal_id": role_id(org, "viewer"),
                                                "privilege": "read"}, format="json")
    assert r.status_code == 201
    listed = owner_client.get(url(org, ds, "acl")).json()["items"]
    assert len(listed) == 2
    assert owner_client.get(url(org, uuid4(), "acl")).json()["items"] == []  # scoped to the dataset
    assert owner_client.delete(url(org, ds, f"acl/{acl['id']}")).status_code == 204
    assert owner_client.delete(url(org, uuid4(), f"acl/{listed[0]['id']}")).status_code == 404
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="governance.acl.create").count() == 2
        assert AuditEvent.objects.filter(action="governance.acl.delete").count() == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"principal_type": "everyone", "privilege": "read"},
        {"principal_type": "role", "principal_id": str(uuid4()), "privilege": "read"},
        {"principal_type": "user", "principal_id": str(uuid4()), "privilege": "read"},
        {"principal_type": "user", "privilege": "read"},
        {"principal_type": "role", "principal_id": "x", "privilege": "read"},
        {"principal_type": "role", "principal_id": str(uuid4()), "privilege": "god"},
    ],
)
def test_acl_validation(org, owner_client, ds, payload):
    assert owner_client.post(url(org, ds, "acl"), payload, format="json").status_code == 400


def test_acl_principal_from_other_org_rejected(org, other_org, owner_client, ds):
    foreign_role = role_id(other_org, "viewer")
    r = owner_client.post(url(org, ds, "acl"), {"principal_type": "role", "principal_id": foreign_role,
                                                "privilege": "read"}, format="json")
    assert r.status_code == 400


def test_governance_permissions_and_isolation(org, other_org, owner, owner_client, ds):
    analyst = client_for(member_of(org, "analyst"))
    for path in ("acl", "column-policies", "row-policies"):
        assert analyst.get(url(org, ds, path)).status_code == 403
        assert owner_client.get(url(other_org, ds, path)).status_code == 404
        assert APIClient().get(url(org, ds, path)).status_code == 401
    with tenant_context(org.id):
        _, token = create_api_key(name="k", scopes=["org.view", "datasets.view"], created_by=owner)
    assert APIClient().get(url(org, ds, "acl"), HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 403


def test_unknown_dataset_is_404_when_datasets_registered(org, owner_client, ds):
    registry._exists["dataset"] = lambda asset_id: asset_id == ds  # restored by the `ds` fixture's monkeypatch
    assert owner_client.get(url(org, ds, "acl")).status_code == 200
    assert owner_client.get(url(org, uuid4(), "acl")).status_code == 404
    assert owner_client.post(url(org, uuid4(), "policy-preview"), {"user_id": str(uuid4())},
                             format="json").status_code == 404


# --- column policies ----------------------------------------------------------------------------------------------
def test_column_policy_crud_and_validation(org, owner_client, ds):
    base = url(org, ds, "column-policies")
    r = owner_client.post(base, {"column_name": "email", "principal_type": "everyone", "action": "mask_partial",
                                 "params": {"keep_last": 2}}, format="json")
    assert r.status_code == 201, r.content
    pid = r.json()["id"]
    assert r.json()["params"] == {"keep_last": 2} and r.json()["principal_id"] is None
    dup = owner_client.post(base, {"column_name": "email", "principal_type": "everyone", "action": "deny"},
                            format="json")
    assert dup.status_code == 400
    bad = [
        {"column_name": "email", "principal_type": "everyone", "action": "mask_partial", "params": {"keep_last": 99}},
        {"column_name": "phone", "principal_type": "everyone", "action": "deny", "params": {"keep_last": 2}},
        {"column_name": "phone", "principal_type": "everyone", "principal_id": str(uuid4()), "action": "deny"},
        {"column_name": "phone", "principal_type": "role", "action": "deny"},
        {"column_name": "phone", "principal_type": "everyone", "action": "explode"},
        {"column_name": 'ph"one', "principal_type": "everyone", "action": "deny"},
        {"column_name": "phone", "principal_type": "everyone", "action": "deny", "exempt_role_ids": [str(uuid4())]},
    ]
    for payload in bad:
        assert owner_client.post(base, payload, format="json").status_code == 400, payload
    r = owner_client.patch(f"{base}/{pid}", {"action": "mask_hash", "params": {}}, format="json")
    assert r.status_code == 200 and r.json()["action"] == "mask_hash"
    r = owner_client.patch(f"{base}/{pid}", {"exempt_role_ids": [role_id(org, "admin")]}, format="json")
    assert r.status_code == 200 and r.json()["exempt_role_ids"] == [role_id(org, "admin")]
    assert owner_client.get(f"{base}/{pid}").status_code == 200
    assert owner_client.delete(f"{base}/{pid}").status_code == 204
    with tenant_context(org.id):
        actions = set(AuditEvent.objects.filter(action__startswith="governance.column_policy")
                      .values_list("action", flat=True))
    assert actions == {"governance.column_policy.create", "governance.column_policy.update",
                       "governance.column_policy.delete"}


# --- row policies -------------------------------------------------------------------------------------------------
def test_row_policy_crud_and_validation(org, owner_client, ds):
    base = url(org, ds, "row-policies")
    pred = {"all": [{"col": "region", "op": "in", "value": "$user.regions"}]}
    r = owner_client.post(base, {"name": "Own regions", "principal_type": "role",
                                 "principal_id": role_id(org, "analyst"), "predicate": pred}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["predicate"] == {"all": [{"col": "region", "op": "in", "value": "$user.regions"}]}
    assert r.json()["is_active"] is True
    pid = r.json()["id"]
    bad = owner_client.post(base, {"name": "Bad", "principal_type": "everyone",
                                   "predicate": {"all": [{"col": "x", "op": "like", "value": "%"}]}}, format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "predicate"
    assert owner_client.patch(f"{base}/{pid}", {"is_active": False}, format="json").json()["is_active"] is False
    assert owner_client.patch(f"{base}/{pid}", {"predicate": {"all": []}}, format="json").status_code == 400
    assert owner_client.delete(f"{base}/{pid}").status_code == 204


# --- user attributes ----------------------------------------------------------------------------------------------
def test_user_attributes(org, other_org, owner_client):
    analyst = member_of(org, "analyst")
    path = org_url(org, f"users/{analyst.id}/attributes")
    assert owner_client.get(path).json() == {"user_id": str(analyst.id), "attributes": {}}
    r = owner_client.put(path, {"attributes": {"regions": ["north", "south", "north"], "cost_center": ["cc-1"]}},
                         format="json")
    assert r.status_code == 200
    assert r.json()["attributes"] == {"cost_center": ["cc-1"], "regions": ["north", "south"]}
    r = owner_client.put(path, {"attributes": {"regions": ["east"]}}, format="json")
    assert r.json()["attributes"] == {"regions": ["east"]}
    assert owner_client.put(path, {"attributes": {"id": ["x"]}}, format="json").status_code == 400
    assert owner_client.put(path, {"attributes": {"Bad Key": ["x"]}}, format="json").status_code == 400
    outsider = make_user()
    assert owner_client.get(org_url(org, f"users/{outsider.id}/attributes")).status_code == 404
    assert owner_client.get(org_url(other_org, f"users/{analyst.id}/attributes")).status_code == 404
    assert client_for(analyst).get(path).status_code == 403
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="governance.user_attributes.update").count() == 2
    with tenant_context(other_org.id):
        assert not UserAttribute.objects.exists()


# --- effective_policy (DB) -------------------------------------------------------------------------------------------
def test_effective_policy_end_to_end(org, owner, ds):
    analyst = member_of(org, "analyst")
    viewer = member_of(org, "viewer")
    analyst_role = role_id(org, "analyst")
    with tenant_context(org.id):
        ColumnPolicy.objects.create(dataset_id=ds, column_name="email", principal_type="everyone", action="mask_hash")
        ColumnPolicy.objects.create(dataset_id=ds, column_name="amount", principal_type="role",
                                    principal_id=analyst_role, action="deny")
        RowPolicy.objects.create(dataset_id=ds, name="regions", principal_type="everyone",
                                 predicate={"all": [{"col": "region", "op": "=", "value": "$user.regions"}]},
                                 exempt_role_ids=[Role.objects.get(key="owner").id])
        UserAttribute.objects.create(user_id=analyst.id, key="regions", values=["north", "west"])

        p = services.effective_policy(analyst.id, ds, TABLES, COLUMNS)
        assert p.allowed_columns["sales"] == frozenset({"order_id", "region", "email"})
        assert p.column_masks[("sales", "email")].kind == "mask_hash"
        assert p.row_predicates["sales"].conditions == (("region", "in", ("north", "west")),)
        assert p.org_id == org.id and p.user_id == analyst.id

        pv = services.effective_policy(viewer.id, ds, TABLES, COLUMNS)  # no attribute -> no rows
        assert pv.row_predicates["sales"].conditions[0][1:] == ("in", ())
        assert "amount" in pv.allowed_columns["sales"]

        po = services.effective_policy(owner.id, ds, TABLES, COLUMNS)  # owner exempt from the row policy only
        assert po.row_predicates == {}
        assert ("sales", "email") in po.column_masks

        assert services.effective_policy(analyst.id, ds, TABLES, COLUMNS).fingerprint == p.fingerprint
        UserAttribute.objects.filter(user_id=analyst.id).update(values=["north"])
        assert services.effective_policy(analyst.id, ds, TABLES, COLUMNS).fingerprint != p.fingerprint


def test_effective_policy_acl(org, owner, ds):
    analyst = member_of(org, "analyst")
    viewer = member_of(org, "viewer")
    with tenant_context(org.id):
        assert services.check_dataset_access(analyst.id, ds, "query")  # no ACL rows: open
        DatasetAcl.objects.create(dataset_id=ds, principal_type="user", principal_id=viewer.id, privilege="read")
        assert not services.check_dataset_access(analyst.id, ds, "read")
        with pytest.raises(services.DatasetAccessDenied):
            services.effective_policy(analyst.id, ds, TABLES, COLUMNS)
        assert services.check_dataset_access(viewer.id, ds, "read")
        assert not services.check_dataset_access(viewer.id, ds, "query")  # read < query
        DatasetAcl.objects.create(dataset_id=ds, principal_type="role", principal_id=Role.objects.get(key="analyst").id,
                                  privilege="export")
        assert services.check_dataset_access(analyst.id, ds, "query")  # export >= query
        assert services.effective_policy(owner.id, ds, TABLES, COLUMNS)  # owner bypasses ACL
        other_ds = uuid4()
        assert services.visible_dataset_ids(viewer.id, [ds, other_ds]) == {str(ds), str(other_ds)}
        assert services.visible_dataset_ids(viewer.id, [ds, other_ds], "query") == {str(other_ds)}
        outsider = make_user()
        with pytest.raises(services.DatasetAccessDenied):
            services.effective_policy(outsider.id, other_ds, TABLES, COLUMNS)
        Membership.objects.filter(user=analyst).update(status="suspended")
        assert not services.check_dataset_access(analyst.id, other_ds)


def test_effective_policy_uses_registered_schema_provider(org, owner, ds):
    saved = list(registry._dataset_schema_provider)
    try:
        registry._dataset_schema_provider.clear()
        with tenant_context(org.id), pytest.raises(ValueError):
            services.effective_policy(owner.id, ds)
        registry.register_dataset_schema_provider(lambda dataset_id: (TABLES, COLUMNS))
        with tenant_context(org.id):
            assert services.effective_policy(owner.id, ds).allowed_columns["sales"] == frozenset(COLUMNS["sales"])
    finally:
        registry._dataset_schema_provider[:] = saved


def test_effective_policy_respects_org_row_cap(org, owner, owner_client, ds):
    owner_client.patch(org_url(org), {"settings": {"row_cap": 99999}}, format="json")
    with tenant_context(org.id):
        assert services.effective_policy(owner.id, ds, TABLES, COLUMNS).row_cap == services.MAX_ROW_CAP
    owner_client.patch(org_url(org), {"settings": {"row_cap": 250}}, format="json")
    with tenant_context(org.id):
        assert services.effective_policy(owner.id, ds, TABLES, COLUMNS).row_cap == 250


def test_policies_are_tenant_isolated(org, other_org, ds):
    with tenant_context(org.id):
        ColumnPolicy.objects.create(dataset_id=ds, column_name="email", principal_type="everyone", action="deny")
    with tenant_context(other_org.id):
        owner_b = Membership.objects.get().user
        assert not ColumnPolicy.objects.exists()
        p = services.effective_policy(owner_b.id, ds, TABLES, COLUMNS)
        assert "email" in p.allowed_columns["sales"]


# --- policy preview --------------------------------------------------------------------------------------------------
def test_policy_preview(org, owner_client, ds):
    analyst = member_of(org, "analyst")
    viewer = member_of(org, "viewer")
    base = url(org, ds, "column-policies")
    owner_client.post(base, {"column_name": "email", "principal_type": "everyone", "action": "mask_partial",
                             "params": {"keep_last": 3}}, format="json")
    r = owner_client.post(url(org, ds, "policy-preview"),
                          {"user_id": str(analyst.id), "tables": TABLES, "columns_by_table": COLUMNS}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["access"] is True
    assert body["policy"]["column_masks"] == [{"table": "sales", "column": "email", "kind": "mask_partial",
                                               "keep_last": 3}]
    assert body["policy"]["allowed_columns"]["sales"] == sorted(COLUMNS["sales"])
    owner_client.post(url(org, ds, "acl"), {"principal_type": "user", "principal_id": str(analyst.id),
                                            "privilege": "read"}, format="json")
    r = owner_client.post(url(org, ds, "policy-preview"),
                          {"user_id": str(viewer.id), "tables": TABLES, "columns_by_table": COLUMNS}, format="json")
    assert r.json() == {"access": False, "policy": None}
    assert owner_client.post(url(org, ds, "policy-preview"), {"user_id": str(uuid4())},
                             format="json").status_code == 404
    assert owner_client.post(url(org, ds, "policy-preview"), {"user_id": str(analyst.id), "tables": TABLES},
                             format="json").status_code == 400
    assert client_for(analyst).post(url(org, ds, "policy-preview"), {"user_id": str(analyst.id)},
                                    format="json").status_code == 403


def test_policy_viewsets_store_the_dataset_id_and_audit_every_write(org, owner_client, ds):
    """Regression: the DRF mixins once shadowed DatasetScopedViewSet.perform_* (MRO), so create hit a NULL
    dataset_id (500) and no write was audited."""
    from apps.governance import views

    for cls in (views.DatasetAclViewSet, views.ColumnPolicyViewSet, views.RowPolicyViewSet):
        for name in ("perform_update", "perform_destroy"):
            assert getattr(cls, name).__qualname__.startswith("DatasetScopedViewSet."), (cls, name)
    col = owner_client.post(url(org, ds, "column-policies"),
                            {"column_name": "email", "principal_type": "everyone", "action": "deny"}, format="json")
    row = owner_client.post(url(org, ds, "row-policies"),
                            {"name": "eu", "principal_type": "everyone",
                             "predicate": {"all": [{"col": "region", "op": "=", "value": "EU"}]}}, format="json")
    assert col.status_code == 201 and row.status_code == 201, (col.content, row.content)
    assert col.json()["dataset_id"] == str(ds) and row.json()["dataset_id"] == str(ds)
    assert owner_client.patch(url(org, ds, f"row-policies/{row.json()['id']}"), {"is_active": False},
                              format="json").status_code == 200
    assert owner_client.delete(url(org, ds, f"column-policies/{col.json()['id']}")).status_code == 204
    with tenant_context(org.id):
        actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"governance.column_policy.create", "governance.row_policy.create", "governance.row_policy.update",
            "governance.column_policy.delete"} <= actions
