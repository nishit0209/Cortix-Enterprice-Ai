"""/semantic-models routes (DB-backed): the dataset "Semantic" tab's shapes, changed-fields-only PATCH, publish, metric
CRUD, permissions, dataset ACLs and cross-org 404s. Real Postgres (RLS) + nexora_wh; storage faked; eager ingest."""

from __future__ import annotations

import pytest

from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.datasets.tests.helpers import add_member, csv_bytes, ingest
from apps.semantic.models import SemanticColumn, SemanticModel

from .test_api import SALES, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx(api_client, two_orgs, fake_storage):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    return api_client, alice, org_a, bob, org_b


@pytest.fixture
def sales(ctx, django_capture_on_commit_callbacks):
    client, _alice, org_a, *_ = ctx
    _, ds = ingest(client, org_a, "sales.csv", csv_bytes(SALES), django_capture_on_commit_callbacks, name="Sales 2024")
    assert ds["status"] == "ready", ds["last_error"]
    return ds


def _model(client, org, dataset_id):
    (summary,) = client.get(url(org, "semantic-models"), {"dataset_id": dataset_id}).json()
    return summary, client.get(url(org, f"semantic-models/{summary['id']}")).json()


def test_list_and_detail_use_the_ui_shape(ctx, sales):
    client, _alice, org_a, *_ = ctx
    summary, model = _model(client, org_a, sales["id"])
    assert summary["dataset_id"] == sales["id"] and summary["version"] == 1 and summary["status"] == "published"
    assert "tables" not in summary  # the UI fetches the detail
    assert model["id"] == summary["id"] and model["status"] == "published" and model["model_status"] == "ready"
    (table,) = model["tables"]
    assert table["alias"] == table["name"] == "sales_2024" and table["display_name"]
    cols = {c["column_name"]: c for c in table["columns"]}
    assert cols["revenue"]["role"] == "measure" and cols["customer_email"]["role"] == "pii"
    assert {"display_name", "description", "synonyms", "default_agg", "exposed_to_ai"} <= set(cols["region"])
    assert {m["key"] for m in model["metrics"]} >= {"row_count", "total_revenue"}
    listed = client.get(url(org_a, "semantic-models")).json()
    assert [m["id"] for m in listed] == [summary["id"]]


def test_patch_with_the_full_table_list_only_records_real_edits(ctx, sales):
    client, _alice, org_a, *_ = ctx
    _summary, model = _model(client, org_a, sales["id"])
    table = model["tables"][0]
    for col in table["columns"]:
        if col["column_name"] == "region":
            col["description"] = "Sales territory"
            col["synonyms"] = ["patch zone", "patch territory"]
        if col["column_name"] == "unit_price":
            col["role"] = "attribute"  # UI alias of dimension
    resp = client.patch(url(org_a, f"semantic-models/{model['id']}"), {"tables": [table]}, format="json")
    assert resp.status_code == 200, resp.content
    cols = {c["column_name"]: c for c in resp.json()["tables"][0]["columns"]}
    assert cols["region"]["description"] == "Sales territory"
    assert cols["region"]["synonyms"] == ["patch zone", "patch territory"]
    assert cols["unit_price"]["role"] == "dimension"
    with tenant_context(org_a.id):
        edited = {c.name: set(c.edited_fields or ()) for c in SemanticColumn.objects.filter(table__model_id=model["id"])}
    assert edited["region"] == {"description", "synonyms"} and edited["unit_price"] == {"role"}
    assert all(not fields for name, fields in edited.items() if name not in ("region", "unit_price"))
    bad = client.patch(url(org_a, f"semantic-models/{model['id']}"),
                       {"tables": [{"alias": "ghost", "columns": []}]}, format="json")
    assert bad.status_code == 400


def test_create_generate_and_publish(ctx, sales):
    client, _alice, org_a, *_ = ctx
    created = client.post(url(org_a, "semantic-models"), {"dataset_id": sales["id"], "name": "Sales model"},
                          format="json")
    assert created.status_code == 200 and created.json()["name"] == "Sales model"  # the model already existed
    mid = created.json()["id"]
    regenerated = client.post(url(org_a, f"semantic-models/{mid}/generate"), {"reset": True}, format="json")
    assert regenerated.status_code == 200 and regenerated.json()["tables"][0]["alias"] == "sales_2024"
    published = client.post(url(org_a, f"semantic-models/{mid}/publish"), {}, format="json")
    assert published.status_code == 200 and published.json()["status"] == "published"
    with tenant_context(org_a.id):
        event = AuditEvent.objects.filter(action="semantic.model.publish", target_id=mid).get()
    assert event.metadata["dataset_version_id"] == sales["current_version"]["id"]
    assert len(event.metadata["schema_hash"]) == 64


def test_metric_crud_through_the_model_routes(ctx, sales):
    client, _alice, org_a, *_ = ctx
    summary, _model_body = _model(client, org_a, sales["id"])
    base = url(org_a, f"semantic-models/{summary['id']}/metrics")
    created = client.post(base, {"key": "", "name": "Average order value", "description": "",
                                 "expression": "sum(revenue) / nullif(count(distinct order_id), 0)",
                                 "synonyms": ["AOV"]}, format="json")
    assert created.status_code == 201, created.content
    metric = created.json()
    assert metric["key"] == "average_order_value" and metric["synonyms"] == ["aov"]
    detail = f"{base}/{metric['id']}"
    patched = client.patch(detail, {**metric, "expression": "avg(revenue)"}, format="json")
    assert patched.status_code == 200 and patched.json()["expression"] == "AVG(revenue)"
    assert patched.json()["key"] == "average_order_value"  # keys are immutable
    assert client.get(detail).json()["id"] == metric["id"]
    assert metric["id"] in {m["id"] for m in client.get(base).json()}
    assert client.post(base, {"name": "Bad", "expression": "pg_sleep(1)"}, format="json").status_code == 400
    assert client.delete(detail).status_code == 204 and client.get(detail).status_code == 404


def test_permissions_acl_and_cross_org(ctx, sales):
    from apps.governance.models import DatasetAcl
    from apps.orgs.models import Role

    client, _alice, org_a, bob, org_b = ctx
    summary, model = _model(client, org_a, sales["id"])
    viewer = add_member(org_a, "viewer-semroutes@example.test", "viewer")
    client.force_authenticate(viewer)
    assert client.get(url(org_a, f"semantic-models/{summary['id']}")).status_code == 200
    assert client.patch(url(org_a, f"semantic-models/{summary['id']}"), {"description": "x"},
                        format="json").status_code == 403
    assert client.post(url(org_a, f"semantic-models/{summary['id']}/publish"), {}, format="json").status_code == 403
    analyst = add_member(org_a, "analyst-semroutes@example.test", "analyst")
    with tenant_context(org_a.id):
        admin_role = Role.objects.get(key="admin")
        DatasetAcl.objects.create(dataset_id=sales["id"], principal_type="role", principal_id=admin_role.id,
                                  privilege="read")
    client.force_authenticate(analyst)
    assert client.get(url(org_a, f"semantic-models/{summary['id']}")).status_code == 404
    assert client.get(url(org_a, "semantic-models")).json() == []
    client.force_authenticate(bob)
    metric_id = model["metrics"][0]["id"]
    for path in (f"semantic-models/{summary['id']}", f"semantic-models/{summary['id']}/metrics",
                 f"semantic-models/{summary['id']}/metrics/{metric_id}"):
        assert client.get(url(org_b, path)).status_code == 404, path
        assert client.get(url(org_a, path)).status_code == 404, path
    assert client.get(url(org_b, "semantic-models")).json() == []
    with tenant_context(org_b.id):
        assert SemanticModel.objects.count() == 0
