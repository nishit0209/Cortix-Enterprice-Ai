"""Lineage edges and graph traversal (real DB, RLS on)."""

import uuid

import pytest

from apps.core.tenancy import TenantContextMissing, tenant_context
from apps.datasets.models import Dataset, DatasetVersion, Upload
from apps.lineage import services
from apps.lineage.models import LineageEdge

pytestmark = pytest.mark.django_db


def make_dataset(org, name="Sales"):
    with tenant_context(org.id):
        upload = Upload.objects.create(filename=f"{name}.csv", content_type="text/csv", file_kind="csv", size_bytes=10,
                                       sha256="0" * 64, object_key=f"orgs/{org.id}/uploads/x/{name}.csv", status="ready")
        ds = Dataset.objects.create(name=name, slug=name.lower(), status="ready")
        v1 = DatasetVersion.objects.create(dataset=ds, version=1, upload=upload, status="ready", wh_table="ds_x_v1")
        services.record("upload", upload.id, "dataset_version", v1.id, label="ingested")
        return upload, ds, v1


def test_record_is_idempotent_and_validates_types(two_orgs):
    _, org_a, _, _ = two_orgs
    a, b = uuid.uuid4(), uuid.uuid4()
    with tenant_context(org_a.id):
        e1 = services.record("upload", a, "dataset_version", b, label="ingested")
        e2 = services.record("upload", str(a), "dataset_version", str(b), label="ingested")
        assert e1.id == e2.id and LineageEdge.objects.count() == 1
        run = uuid.uuid4()
        services.record("upload", a, "dataset_version", b, run_id=run)
        assert LineageEdge.objects.count() == 2
        with pytest.raises(ValueError):
            services.record("bogus", a, "dataset_version", b)
    with pytest.raises(TenantContextMissing):
        services.record("upload", a, "dataset_version", b)


def test_graph_from_dataset_expands_versions_and_uploads(two_orgs):
    _, org_a, _, _ = two_orgs
    upload, ds, v1 = make_dataset(org_a)
    pipeline_version = uuid.uuid4()
    with tenant_context(org_a.id):
        services.record("dataset_version", v1.id, "pipeline_version", pipeline_version, label="input")
        g = services.graph("dataset", ds.id, depth=3)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[str(ds.id)]["label"] == "Sales" and nodes[str(v1.id)]["label"] == "Sales v1"
    assert nodes[str(upload.id)]["type"] == "upload"
    assert nodes[str(pipeline_version)]["type"] == "pipeline_version"  # no resolver yet: shown generically
    edges = {(e["from"], e["to"], e["label"]) for e in g["edges"]}
    assert (str(upload.id), str(v1.id), "ingested") in edges
    assert (str(v1.id), str(ds.id), "version of") in edges
    assert (str(v1.id), str(pipeline_version), "input") in edges


def test_deleted_assets_collapse_into_restricted_placeholder(two_orgs):
    _, org_a, _, _ = two_orgs
    upload, ds, v1 = make_dataset(org_a, "Gone")
    _, other, ov1 = make_dataset(org_a, "Kept")
    with tenant_context(org_a.id):
        services.record("dataset_version", v1.id, "dataset_version", ov1.id, label="derived")
        Dataset.objects.filter(id=ds.id).update(status="deleted")
        g = services.graph("dataset", other.id)
    ids = {n["id"] for n in g["nodes"]}
    assert str(v1.id) not in ids and "restricted" in ids
    assert any(e["from"] == "restricted" and e["to"] == str(ov1.id) for e in g["edges"])
    with tenant_context(org_a.id):
        assert services.graph("dataset", ds.id) == {"nodes": [], "edges": []}


def test_graph_is_tenant_isolated(two_orgs):
    _, org_a, _, org_b = two_orgs
    _, ds, v1 = make_dataset(org_a)
    with tenant_context(org_b.id):
        assert services.graph("dataset", ds.id) == {"nodes": [], "edges": []}
        assert services.graph("dataset_version", v1.id) == {"nodes": [], "edges": []}
        assert LineageEdge.objects.count() == 0


def test_lineage_api_validation_and_404(api_client, two_orgs):
    alice, org_a, bob, org_b = two_orgs
    _, ds, _ = make_dataset(org_a)
    api_client.force_authenticate(alice)
    base = f"/api/v1/orgs/{org_a.slug}/lineage"
    ok = api_client.get(f"{base}?node_type=dataset&node_id={ds.id}")
    assert ok.status_code == 200 and ok.json()["nodes"]
    assert api_client.get(f"{base}?node_type=nope&node_id={ds.id}").status_code == 400
    assert api_client.get(f"{base}?node_type=dataset&node_id=not-a-uuid").status_code == 400
    assert api_client.get(f"{base}?node_type=dataset&node_id={uuid.uuid4()}").status_code == 404
    api_client.force_authenticate(bob)
    assert api_client.get(f"/api/v1/orgs/{org_b.slug}/lineage?node_type=dataset&node_id={ds.id}").status_code == 404
    assert api_client.get(f"{base}?node_type=dataset&node_id={ds.id}").status_code == 404
