"""W2: preview applies governance (column deny/mask, row policies, ACL) through the SQL guard; registry providers.

Real Postgres (nexora_app + RLS, nexora_wh); object storage is an in-memory fake; ingestion runs eagerly.
"""

from __future__ import annotations

import pytest

from apps.core.registry import asset_exists, classify_asset, dataset_schema
from apps.core.tenancy import tenant_context
from apps.datasets.tests.helpers import SAMPLE_CUSTOMERS, add_member, csv_bytes, ingest
from apps.governance.models import ColumnPolicy, DatasetAcl, RowPolicy

pytestmark = pytest.mark.django_db


def url(org, path=""):
    return f"/api/v1/orgs/{org.slug}/{path}"


@pytest.fixture
def ctx(api_client, two_orgs, fake_storage):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    return api_client, alice, org_a, bob, org_b


@pytest.fixture
def customers(ctx, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    _, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    assert ds["status"] == "ready", ds["last_error"]
    return ds


def test_registry_providers(ctx, customers):
    _, _, org_a, _, org_b = ctx
    with tenant_context(org_a.id):
        tables, columns = dataset_schema(customers["id"])
        assert list(tables) == ["customers"]
        assert tables["customers"].endswith("." + customers["current_version"]["wh_table"])
        assert columns["customers"][0] == "customer_id" and "email" in columns["customers"]
        assert asset_exists("dataset", customers["id"]) is True
        assert classify_asset(f"dataset:{customers['id']}") == "internal"
    with tenant_context(org_b.id):
        assert asset_exists("dataset", customers["id"]) is False
        assert classify_asset(f"dataset:{customers['id']}") == "confidential"


def test_preview_without_policies_shows_all_columns(ctx, customers):
    client, _, org_a, *_ = ctx
    body = client.get(url(org_a, f"datasets/{customers['id']}/preview?limit=5")).json()
    assert [c["name"] for c in body["columns"]][:3] == ["customer_id", "full_name", "email"]
    assert body["row_count"] == 5 and body["truncated"] is True
    assert not any(c["masked"] for c in body["columns"])


def test_preview_applies_column_deny_and_masks_for_the_user(ctx, customers):
    client, alice, org_a, *_ = ctx
    analyst = add_member(org_a, "analyst-prev@example.test", "analyst")
    with tenant_context(org_a.id):
        ColumnPolicy.objects.create(dataset_id=customers["id"], column_name="email", principal_type="everyone",
                                    action="deny")
        ColumnPolicy.objects.create(dataset_id=customers["id"], column_name="full_name", principal_type="everyone",
                                    action="mask_partial", params={"keep_last": 2})
    client.force_authenticate(analyst)
    body = client.get(url(org_a, f"datasets/{customers['id']}/preview?limit=3")).json()
    names = [c["name"] for c in body["columns"]]
    assert "email" not in names
    full_name = names.index("full_name")
    assert body["columns"][full_name]["masked"] is True and body["columns"][full_name]["type"] == "text"
    assert all(str(r[full_name]).startswith("*") for r in body["rows"])
    assert not any("@example.test" in str(v) for r in body["rows"] for v in r)


def test_preview_applies_row_policies(ctx, customers):
    client, _, org_a, *_ = ctx
    analyst = add_member(org_a, "analyst-rows@example.test", "analyst")
    with tenant_context(org_a.id):
        RowPolicy.objects.create(dataset_id=customers["id"], name="pune only", principal_type="everyone",
                                 predicate={"all": [{"col": "city", "op": "=", "value": "Pune"}]})
    client.force_authenticate(analyst)
    body = client.get(url(org_a, f"datasets/{customers['id']}/preview?limit=100")).json()
    city = [c["name"] for c in body["columns"]].index("city")
    assert body["row_count"] == 10 and {r[city] for r in body["rows"]} == {"Pune"}


def test_preview_respects_the_dataset_acl(ctx, customers):
    from apps.orgs.models import Role

    client, _, org_a, *_ = ctx
    viewer = add_member(org_a, "viewer-acl@example.test", "viewer")
    with tenant_context(org_a.id):
        DatasetAcl.objects.create(dataset_id=customers["id"], principal_type="role",
                                  principal_id=Role.objects.get(key="analyst").id, privilege="read")
    client.force_authenticate(viewer)
    assert client.get(url(org_a, f"datasets/{customers['id']}/preview")).status_code == 404


def test_preview_with_every_column_denied_is_empty(ctx, customers):
    client, alice, org_a, *_ = ctx
    viewer = add_member(org_a, "viewer-deny@example.test", "viewer")
    names = [c["name"] for c in client.get(url(org_a, f"datasets/{customers['id']}/columns")).json()]
    with tenant_context(org_a.id):
        for col in names:
            ColumnPolicy.objects.create(dataset_id=customers["id"], column_name=col, principal_type="everyone",
                                        action="deny")
    client.force_authenticate(viewer)
    body = client.get(url(org_a, f"datasets/{customers['id']}/preview")).json()
    assert body["columns"] == [] and body["rows"] == []


def test_guarded_query_task_body_applies_the_users_policy(ctx, customers):
    from apps.sqlguard.tasks import run_guarded_query, run_query_ext

    client, alice, org_a, *_ = ctx
    analyst = add_member(org_a, "analyst-task@example.test", "analyst")
    with tenant_context(org_a.id):
        ColumnPolicy.objects.create(dataset_id=customers["id"], column_name="email", principal_type="everyone",
                                    action="deny")
        RowPolicy.objects.create(dataset_id=customers["id"], name="delhi", principal_type="everyone",
                                 predicate={"all": [{"col": "city", "op": "=", "value": "Delhi"}]})
        ok = run_guarded_query(org_id=org_a.id, user_id=analyst.pk, dataset_id=customers["id"],
                               sql="SELECT city, count(*) AS n FROM customers GROUP BY city")
        denied = run_guarded_query(org_id=org_a.id, user_id=analyst.pk, dataset_id=customers["id"],
                                   sql="SELECT email FROM customers")
        owner = run_guarded_query(org_id=org_a.id, user_id=alice.pk, dataset_id=customers["id"],
                                  sql="SELECT count(*) AS n FROM customers")
    assert ok["ok"] and ok["rows"] == [["Delhi", 10]] and ok["columns"][0]["name"] == "city"
    assert denied == {"ok": False, "reject_code": "column_not_allowed", "message": denied["message"],
                      "fingerprint": denied["fingerprint"]}
    assert owner["ok"] and owner["rows"] == [[10]]  # everyone-policies apply to owners too (no exemption)
    assert run_query_ext.run(org_id=str(org_a.id), user_id=str(alice.pk), dataset_id=customers["id"],
                             sql="SELECT 1")["reject_code"] == "execution_error"
