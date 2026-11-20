"""Semantic layer: generated on ingestion, dataset_semantics contract, model/metric API, permissions, isolation.

Real Postgres (nexora_app + RLS, nexora_wh); object storage is an in-memory fake; ingestion runs eagerly.
"""

from __future__ import annotations

import pytest

from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.datasets.tests.helpers import SAMPLE_CUSTOMERS, add_member, csv_bytes, ingest, upload_file
from apps.semantic import services
from apps.semantic.models import MetricDefinition, SemanticColumn, SemanticModel
from nexora.contracts import DatasetSemantics

pytestmark = pytest.mark.django_db

SALES = [
    ["Order ID", "Order Date", "Region", "Revenue", "Unit Price", "Customer Email"],
    *[[i, f"2024-02-{(i % 27) + 1:02d}", ["North", "South"][i % 2], f"{100 + i}.50", f"{10 + i % 5}.00",
       f"buyer{i}@example.test"] for i in range(30)],
]


def url(org, path=""):
    return f"/api/v1/orgs/{org.slug}/{path}"


@pytest.fixture
def ctx(api_client, two_orgs, fake_storage):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    return api_client, alice, org_a, bob, org_b


@pytest.fixture
def sales(ctx, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    _, ds = ingest(client, org_a, "sales.csv", csv_bytes(SALES), django_capture_on_commit_callbacks, name="Sales 2024")
    assert ds["status"] == "ready", ds["last_error"]
    return ds


def test_model_is_generated_on_ingestion(ctx, sales):
    _, _, org_a, *_ = ctx
    with tenant_context(org_a.id):
        model = SemanticModel.objects.get(dataset_id=sales["id"])
        assert str(model.dataset_version_id) == sales["current_version"]["id"]
        roles = dict(SemanticColumn.objects.filter(table__model=model).values_list("name", "role"))
    assert roles["order_id"] == "id" and roles["order_date"] == "time" and roles["region"] == "dimension"
    assert roles["revenue"] == "measure" and roles["customer_email"] == "pii"


def test_dataset_semantics_contract(ctx, sales):
    _, _, org_a, *_ = ctx
    with tenant_context(org_a.id):
        sem = services.dataset_semantics(sales["id"])
    assert isinstance(sem, DatasetSemantics)
    assert list(sem.tables) == ["sales_2024"]
    schema, table = sem.tables["sales_2024"].split(".")
    assert table == sales["current_version"]["wh_table"] and schema.endswith(org_a.short_id)
    assert sem.columns_by_table["sales_2024"] == ("order_id", "order_date", "region", "revenue", "unit_price",
                                                   "customer_email")
    meta = {m["name"]: m for m in sem.columns_meta["sales_2024"]}
    assert {"name", "type", "semantic_type", "role", "description", "synonyms"} <= set(meta["revenue"])
    assert "sales" in meta["revenue"]["synonyms"] or "turnover" in meta["revenue"]["synonyms"]
    assert meta["customer_email"]["is_pii"] is True and meta["customer_email"]["synonyms"] == []
    keys = {m["key"] for m in sem.metrics}
    assert {"row_count", "total_revenue", "avg_revenue", "avg_unit_price"} <= keys
    assert all({"key", "name", "expression", "description", "synonyms"} <= set(m) for m in sem.metrics)
    assert len(sem.schema_hash) == 64 and sem.dataset_name == "Sales 2024" and sem.classification == "internal"


def test_get_patch_and_edits_survive_a_new_version(ctx, sales, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    body = client.get(url(org_a, f"datasets/{sales['id']}/semantic-model")).json()
    assert body["tables"][0]["name"] == "sales_2024" and body["metrics"]
    patch = {"description": "Synthetic sales", "columns": [
        {"name": "region", "description": "Sales territory", "synonyms": ["Zone", "territory", "zone"]},
        {"name": "unit_price", "role": "dimension", "default_agg": None},
    ]}
    resp = client.patch(url(org_a, f"datasets/{sales['id']}/semantic-model"), patch, format="json")
    assert resp.status_code == 200, resp.content
    cols = {c["name"]: c for c in resp.json()["tables"][0]["columns"]}
    assert cols["region"]["description"] == "Sales territory" and cols["region"]["synonyms"] == ["zone", "territory"]
    assert cols["unit_price"]["role"] == "dimension" and "role" in cols["unit_price"]["edited_fields"]
    # a new version regenerates the model but keeps the edits
    up = upload_file(client, org_a, "sales2.csv", csv_bytes(SALES[:10])).json()
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post(url(org_a, f"datasets/{sales['id']}/versions"), {"upload_id": up["id"]},
                           format="json").status_code == 202
    body = client.get(url(org_a, f"datasets/{sales['id']}/semantic-model")).json()
    assert body["version"] == 2
    cols = {c["name"]: c for c in body["tables"][0]["columns"]}
    assert cols["region"]["description"] == "Sales territory" and cols["unit_price"]["role"] == "dimension"
    with tenant_context(org_a.id):
        assert AuditEvent.objects.filter(action="semantic.model.update").exists()
    # reset discards the edits
    resp = client.post(url(org_a, f"datasets/{sales['id']}/semantic-model/refresh"), {"reset": True}, format="json")
    cols = {c["name"]: c for c in resp.json()["tables"][0]["columns"]}
    assert cols["region"]["description"] != "Sales territory" and cols["unit_price"]["role"] == "measure"


def test_pii_role_cannot_be_removed(ctx, sales):
    client, _, org_a, *_ = ctx
    resp = client.patch(url(org_a, f"datasets/{sales['id']}/semantic-model"),
                        {"columns": [{"name": "customer_email", "role": "dimension"}]}, format="json")
    assert resp.status_code == 400


def test_metric_crud_and_validation(ctx, sales):
    client, _, org_a, *_ = ctx
    base = url(org_a, f"datasets/{sales['id']}/metrics")
    resp = client.post(base, {"name": "Average order value",
                              "expression": "sum(revenue) / nullif(count(distinct order_id), 0)",
                              "synonyms": ["AOV", "basket size"]}, format="json")
    assert resp.status_code == 201, resp.content
    metric = resp.json()
    assert metric["key"] == "average_order_value"
    assert metric["expression"] == "SUM(revenue) / NULLIF(COUNT(DISTINCT order_id), 0)"
    assert metric["synonyms"] == ["aov", "basket size"]
    for bad in ("pg_sleep(1)", "sum(secret_col)", "revenue", "(SELECT 1)", "sum(revenue); DROP TABLE x"):
        assert client.post(base, {"name": "Bad", "expression": bad}, format="json").status_code == 400, bad
    assert client.post(base, {"name": "Average order value", "expression": "sum(revenue)"},
                       format="json").status_code == 400  # duplicate key
    detail = f"{base}/{metric['id']}"
    resp = client.patch(detail, {"expression": "avg(revenue)"}, format="json")
    assert resp.status_code == 200 and resp.json()["expression"] == "AVG(revenue)"
    assert client.delete(detail).status_code == 204
    assert client.get(detail).status_code == 404
    # deleting a generated default deactivates it (a refresh does not bring it back)
    default = next(m for m in client.get(base).json() if m["key"] == "total_revenue")
    assert client.delete(f"{base}/{default['id']}").status_code == 204
    client.post(url(org_a, f"datasets/{sales['id']}/semantic-model/refresh"), {}, format="json")
    with tenant_context(org_a.id):
        assert MetricDefinition.objects.get(id=default["id"]).is_active is False
        assert "total_revenue" not in {m["key"] for m in services.dataset_semantics(sales["id"]).metrics}


def test_viewer_can_read_but_not_write(ctx, sales):
    client, _, org_a, *_ = ctx
    viewer = add_member(org_a, "viewer-sem@example.test", "viewer")
    client.force_authenticate(viewer)
    assert client.get(url(org_a, f"datasets/{sales['id']}/semantic-model")).status_code == 200
    assert client.get(url(org_a, f"datasets/{sales['id']}/metrics")).status_code == 200
    assert client.patch(url(org_a, f"datasets/{sales['id']}/semantic-model"), {"description": "x"},
                        format="json").status_code == 403
    assert client.post(url(org_a, f"datasets/{sales['id']}/metrics"), {"name": "x", "expression": "count(*)"},
                       format="json").status_code == 403


def test_dataset_acl_hides_the_model(ctx, sales):
    from apps.governance.models import DatasetAcl
    from apps.orgs.models import Role

    client, alice, org_a, *_ = ctx
    analyst = add_member(org_a, "analyst-sem@example.test", "analyst")
    with tenant_context(org_a.id):
        admin_role = Role.objects.get(key="admin")
        DatasetAcl.objects.create(dataset_id=sales["id"], principal_type="role", principal_id=admin_role.id,
                                  privilege="read")
    client.force_authenticate(analyst)
    assert client.get(url(org_a, f"datasets/{sales['id']}/semantic-model")).status_code == 404


def test_not_ready_dataset_is_409(ctx):
    client, _, org_a, *_ = ctx
    up = upload_file(client, org_a, "q.csv", b"a\n1\n").json()
    pending = client.post(url(org_a, "datasets"), {"name": "Pending", "upload_id": up["id"]}, format="json").json()
    resp = client.get(url(org_a, f"datasets/{pending['dataset']['id']}/semantic-model"))
    assert resp.status_code == 409 and resp.json()["code"] == "dataset_not_ready"


@pytest.mark.ac("P3-AC6")
def test_cross_org_is_404(ctx, sales):
    client, alice, org_a, bob, org_b = ctx
    with tenant_context(org_a.id):
        metric_id = MetricDefinition.objects.filter(model__dataset_id=sales["id"]).first().id
    client.force_authenticate(bob)
    for path in (f"datasets/{sales['id']}/semantic-model", f"datasets/{sales['id']}/metrics",
                 f"datasets/{sales['id']}/metrics/{metric_id}"):
        assert client.get(url(org_b, path)).status_code == 404, path
        assert client.get(url(org_a, path)).status_code == 404, path
    assert client.patch(url(org_b, f"datasets/{sales['id']}/semantic-model"), {"description": "x"},
                        format="json").status_code == 404
    with tenant_context(org_b.id):
        assert SemanticModel.objects.count() == 0


def test_ingest_of_customers_sample_builds_pii_aware_model(ctx, django_capture_on_commit_callbacks):
    client, _, org_a, *_ = ctx
    _, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    with tenant_context(org_a.id):
        sem = services.dataset_semantics(ds["id"])
    meta = {m["name"]: m for m in sem.columns_meta[next(iter(sem.tables))]}
    assert meta["email"]["role"] == "pii" and meta["email"]["is_pii"]
    assert not any("email" in m["expression"] for m in sem.metrics)
