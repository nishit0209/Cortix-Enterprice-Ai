"""Dataset/upload API tests: full ingest flow, rejections, permissions, classification, cross-org 404.

Real Postgres (nexora_app under RLS + nexora_wh); object storage is an in-memory fake; the ingest task runs
eagerly once the request's transaction "commits" (django_capture_on_commit_callbacks).
"""

import io
import zipfile

import pytest

from apps.core.models import AuditEvent, Job
from apps.core.registry import classify_asset
from apps.core.tenancy import tenant_context
from apps.datasets.models import Dataset, DatasetVersion, Upload
from apps.datasets.tests.helpers import (
    SAMPLE_CUSTOMERS,
    add_member,
    csv_bytes,
    ingest,
    json_bytes,
    upload_file,
    xlsx_bytes,
)

pytestmark = pytest.mark.django_db


def url(org, path=""):
    return f"/api/v1/orgs/{org.slug}/{path}"


@pytest.fixture
def ctx(api_client, two_orgs, fake_storage):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    return api_client, alice, org_a, bob, org_b


@pytest.fixture
def ready_dataset(ctx, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    resp, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    assert ds["status"] == "ready", ds["last_error"]
    return resp.json(), ds


# --- uploads ------------------------------------------------------------------------------------------
def test_upload_streams_to_org_prefix(ctx, fake_storage):
    client, alice, org_a, *_ = ctx
    data = csv_bytes(SAMPLE_CUSTOMERS)
    resp = upload_file(client, org_a, "customers.csv", data)
    assert resp.status_code == 201
    body = resp.json()
    assert {"id", "filename", "size_bytes", "status"} <= set(body)
    assert body["status"] == "ready" and body["size_bytes"] == len(data) and body["filename"] == "customers.csv"
    assert len(body["sha256"]) == 64
    (key,) = fake_storage.objects
    assert key.startswith(f"orgs/{org_a.id}/uploads/{body['id']}/") and fake_storage.objects[key] == data
    assert client.get(url(org_a, f"uploads/{body['id']}")).status_code == 200


@pytest.mark.parametrize(
    "name,data,status,code",
    [
        ("evil.exe", b"MZ....", 422, "upload_type_not_allowed"),
        ("fake.csv", b"%PDF-1.4 not a csv", 422, "upload_content_mismatch"),
        ("fake.xlsx", b"just,text\n1,2\n", 422, "upload_content_mismatch"),
        ("empty.csv", b"", 422, "upload_empty"),
    ],
)
def test_upload_rejections(ctx, name, data, status, code):
    client, alice, org_a, *_ = ctx
    resp = upload_file(client, org_a, name, data)
    assert resp.status_code == status
    assert resp["Content-Type"].startswith("application/problem+json") and resp.json()["code"] == code


def test_upload_zip_bomb_rejected(ctx):
    client, alice, org_a, *_ = ctx
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", "A" * (30 * 1024 * 1024))
    resp = upload_file(client, org_a, "bomb.xlsx", buf.getvalue())
    assert resp.status_code == 422 and resp.json()["code"] == "upload_zip_bomb"


def test_upload_size_limit(ctx, settings):
    client, alice, org_a, *_ = ctx
    settings.MAX_UPLOAD_BYTES = 10
    resp = upload_file(client, org_a, "a.csv", b"a,b\n1,2\n3,4\n")
    assert resp.status_code == 413 and resp.json()["code"] == "upload_too_large"


def test_upload_requires_file_and_permission(ctx):
    client, alice, org_a, *_ = ctx
    assert client.post(url(org_a, "uploads"), {}, format="multipart").status_code == 400
    viewer = add_member(org_a, "viewer@example.test", "viewer")
    client.force_authenticate(viewer)
    assert upload_file(client, org_a, "a.csv", b"a\n1\n").status_code == 403


# --- full ingest --------------------------------------------------------------------------------------
@pytest.mark.ac("P3-AC1")
def test_csv_ingest_end_to_end(ctx, ready_dataset):
    client, alice, org_a, *_ = ctx
    created, ds = ready_dataset
    assert {"dataset", "job_id", "events_url", "status_url"} <= set(created)
    assert created["events_url"].endswith(f"/jobs/{created['job_id']}/events")
    assert ds["slug"] == "customers" and ds["classification"] == "internal"
    assert ds["row_count"] == 40 and ds["column_count"] == 7
    assert ds["current_version"]["version"] == 1 and ds["current_version"]["status"] == "ready"
    assert ds["current_version"]["wh_table"].startswith("ds_") and ds["current_version"]["wh_table"].endswith("_v1")
    assert 0 < ds["quality_score"] <= 100

    job = client.get(url(org_a, f"jobs/{created['job_id']}")).json()
    assert job["status"] == "succeeded" and float(job["progress"]) == 100
    assert job["result"]["row_count"] == 40 and job["result"]["current"] is True

    cols = {c["name"]: c for c in client.get(url(org_a, f"datasets/{ds['id']}/columns")).json()}
    assert list(cols) == ["customer_id", "full_name", "email", "city", "signup_date", "lifetime_value_inr", "active"]
    assert cols["customer_id"]["data_type"] == "bigint" and cols["customer_id"]["semantic_type"] == "id"
    assert cols["customer_id"]["source_name"] == "Customer ID"
    assert cols["email"]["is_pii"] and cols["email"]["stats"]["top_values"] is None
    assert cols["full_name"]["is_pii"]
    assert cols["city"]["semantic_type"] == "geo_city" and not cols["city"]["is_pii"]
    assert cols["city"]["stats"]["distinct"] == 4 and cols["city"]["stats"]["top_values"]
    assert cols["signup_date"]["data_type"] == "date"
    assert cols["lifetime_value_inr"]["data_type"] == "numeric"
    assert cols["lifetime_value_inr"]["semantic_type"] == "currency"
    assert len(cols["lifetime_value_inr"]["stats"]["histogram"]) == 20
    assert cols["active"]["data_type"] == "boolean"

    preview = client.get(url(org_a, f"datasets/{ds['id']}/preview?limit=5")).json()
    assert [c["name"] for c in preview["columns"]] == list(cols)
    assert len(preview["rows"]) == 5 and preview["rows"][0][0] == 1000
    assert preview["rows"][0][4] == "2024-01-01" and preview["rows"][0][6] is False

    quality = client.get(url(org_a, f"datasets/{ds['id']}/quality")).json()
    assert quality["score"] == ds["quality_score"]
    assert set(quality["dimensions"]) >= {"completeness", "uniqueness", "validity", "consistency"}
    assert quality["rules"] and all(r["passed"] is True for r in quality["rules"])
    assert {"exact_rows", "key_duplicates", "fuzzy_clusters"} <= set(quality["duplicates"])
    assert quality["duplicates"]["exact_rows"] == 0

    lineage = client.get(url(org_a, f"lineage?node_type=dataset&node_id={ds['id']}")).json()
    types = {n["type"] for n in lineage["nodes"]}
    assert {"dataset", "dataset_version", "upload"} <= types
    assert {e["label"] for e in lineage["edges"]} >= {"ingested", "version of"}

    stats = client.get(url(org_a, "data-stats")).json()
    assert stats["datasets"] == 1 and stats["rows_total"] == 40 and stats["storage_bytes"] > 0
    assert stats["avg_quality"] == ds["quality_score"]
    assert stats["runs_7d"] == {"succeeded": 0, "failed": 0, "total": 0}  # W3: `total` added (additive)

    with tenant_context(org_a.id):
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        assert {"upload.create", "dataset.create", "dataset.version.ready"} <= actions
        assert classify_asset(f"dataset:{ds['id']}") == "internal"


def test_xlsx_and_json_ingest(ctx, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    _, xl = ingest(client, org_a, "book.xlsx", xlsx_bytes([["Region", "Units"], ["North", 10], ["South", 3]]),
                   django_capture_on_commit_callbacks, name="Regions")
    assert xl["status"] == "ready" and xl["row_count"] == 2
    records = [{"id": i, "user": {"city": "Pune", "tags": ["a"]}, "score": i * 1.5} for i in range(5)]
    _, js = ingest(client, org_a, "events.json", json_bytes(records), django_capture_on_commit_callbacks, name="Events")
    assert js["status"] == "ready" and js["row_count"] == 5
    cols = {c["name"]: c["data_type"] for c in client.get(url(org_a, f"datasets/{js['id']}/columns")).json()}
    assert cols == {"id": "bigint", "user_city": "text", "user_tags": "jsonb", "score": "double precision"}
    rows = client.get(url(org_a, f"datasets/{js['id']}/preview")).json()["rows"]
    assert rows[0][2] == ["a"]


def test_malformed_file_fails_the_job_with_a_code(ctx, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    resp, ds = ingest(client, org_a, "bad.csv", b"a,b\n1,2\n3,4,5,6\n", django_capture_on_commit_callbacks, name="Bad")
    assert ds["status"] == "failed" and ds["last_error"]["code"] == "malformed_file"
    assert ds["current_version"] is None
    job = client.get(url(org_a, f"jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "failed" and job["error"]["code"] == "malformed_file"
    with tenant_context(org_a.id):
        assert Upload.objects.get().status == "rejected"


def test_new_version_with_drift(ctx, ready_dataset, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    rows = [r[:5] + ["extra"] for r in SAMPLE_CUSTOMERS]
    rows[0][5] = "Note"
    up = upload_file(client, org_a, "v2.csv", csv_bytes(rows)).json()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(org_a, f"datasets/{ds['id']}/versions"), {"upload_id": up["id"]}, format="json")
    assert resp.status_code == 202
    detail = client.get(url(org_a, f"datasets/{ds['id']}")).json()
    assert detail["current_version"]["version"] == 2 and detail["column_count"] == 6
    versions = client.get(url(org_a, f"datasets/{ds['id']}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    with tenant_context(org_a.id):
        drift = DatasetVersion.objects.get(version=2).profile["drift"]
    assert drift["added"] == ["note"] and set(drift["removed"]) == {"lifetime_value_inr", "active"}
    old_cols = client.get(url(org_a, f"datasets/{ds['id']}/columns?version=1")).json()
    assert len(old_cols) == 7


def test_create_dataset_validation(ctx):
    client, alice, org_a, *_ = ctx
    assert client.post(url(org_a, "datasets"), {"name": "x"}, format="json").status_code == 400
    import uuid

    resp = client.post(url(org_a, "datasets"), {"name": "x", "upload_id": str(uuid.uuid4())}, format="json")
    assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "upload_id"
    up = upload_file(client, org_a, "a.csv", b"a\n1\n").json()
    bad = client.post(url(org_a, "datasets"), {"name": "x", "upload_id": up["id"], "options": {"evil": 1}}, format="json")
    assert bad.status_code == 400


# --- dataset management ---------------------------------------------------------------------------------
def test_list_search_patch_delete(ctx, ready_dataset, django_capture_on_commit_callbacks):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    ingest(client, org_a, "o.csv", csv_bytes([["order", "qty"], ["A1", 1]]), django_capture_on_commit_callbacks,
           name="Orders")
    listing = client.get(url(org_a, "datasets")).json()
    assert {"items", "next_cursor", "previous_cursor"} <= set(listing) and len(listing["items"]) == 2
    found = client.get(url(org_a, "datasets?search=custom")).json()["items"]
    assert [d["id"] for d in found] == [ds["id"]]

    patched = client.patch(url(org_a, f"datasets/{ds['id']}"),
                           {"name": "Customer master", "description": "Synthetic customers", "tags": ["crm"]},
                           format="json")
    assert patched.status_code == 200
    assert patched.json()["name"] == "Customer master" and patched.json()["tags"] == ["crm"]
    assert client.get(url(org_a, "datasets?tag=crm")).json()["items"][0]["id"] == ds["id"]
    assert client.patch(url(org_a, f"datasets/{ds['id']}"), {"classification": "public"}, format="json").status_code == 400

    assert client.delete(url(org_a, f"datasets/{ds['id']}")).status_code == 204
    assert client.get(url(org_a, f"datasets/{ds['id']}")).status_code == 404
    assert len(client.get(url(org_a, "datasets")).json()["items"]) == 1
    with tenant_context(org_a.id):
        assert Dataset.objects.get(id=ds["id"]).status == "deleted"
        assert AuditEvent.objects.filter(action="dataset.delete").exists()


def test_classification_requires_attestation_and_is_audited(ctx, ready_dataset):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    path = url(org_a, f"datasets/{ds['id']}/classification")
    no_attest = client.post(path, {"classification": "synthetic"}, format="json")
    assert no_attest.status_code == 422 and no_attest.json()["code"] == "attestation_required"
    ok = client.post(path, {"classification": "synthetic", "attest": True}, format="json")
    assert ok.status_code == 200
    assert ok.json()["classification"] == "synthetic" and ok.json()["classification_attested_at"]
    back = client.post(path, {"classification": "confidential"}, format="json").json()
    assert back["classification"] == "confidential" and back["classification_attested_at"] is None
    with tenant_context(org_a.id):
        assert AuditEvent.objects.filter(action="dataset.classification.change").count() == 2
        assert classify_asset(f"dataset:{ds['id']}") == "confidential"
    assert client.post(path, {"classification": "secret"}, format="json").status_code == 400


def test_classification_permission(ctx, ready_dataset):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    engineer = add_member(org_a, "engineer@example.test", "data_engineer")  # no datasets.classify
    client.force_authenticate(engineer)
    resp = client.post(url(org_a, f"datasets/{ds['id']}/classification"),
                       {"classification": "public", "attest": True}, format="json")
    assert resp.status_code == 403
    up = upload_file(client, org_a, "p.csv", b"a\n1\n").json()
    create = client.post(url(org_a, "datasets"),
                         {"name": "P", "upload_id": up["id"], "classification": "public", "attest": True}, format="json")
    assert create.status_code == 403


def test_create_with_synthetic_classification(ctx):
    client, alice, org_a, *_ = ctx
    up = upload_file(client, org_a, "s.csv", b"a\n1\n").json()
    no = client.post(url(org_a, "datasets"), {"name": "S", "upload_id": up["id"], "classification": "synthetic"},
                     format="json")
    assert no.status_code == 422
    yes = client.post(url(org_a, "datasets"),
                      {"name": "S", "upload_id": up["id"], "classification": "synthetic", "attest": True}, format="json")
    assert yes.status_code == 202 and yes.json()["dataset"]["classification"] == "synthetic"


def test_viewer_can_read_but_not_manage(ctx, ready_dataset):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    viewer = add_member(org_a, "viewer2@example.test", "viewer")
    client.force_authenticate(viewer)
    assert client.get(url(org_a, f"datasets/{ds['id']}")).status_code == 200
    assert client.get(url(org_a, f"datasets/{ds['id']}/preview")).status_code == 200
    assert client.patch(url(org_a, f"datasets/{ds['id']}"), {"name": "x"}, format="json").status_code == 403
    assert client.delete(url(org_a, f"datasets/{ds['id']}")).status_code == 403


def test_preview_limits_and_not_ready(ctx, ready_dataset):
    client, alice, org_a, *_ = ctx
    _, ds = ready_dataset
    assert client.get(url(org_a, f"datasets/{ds['id']}/preview?limit=0")).status_code == 400
    assert client.get(url(org_a, f"datasets/{ds['id']}/preview?limit=5000")).status_code == 400
    up = upload_file(client, org_a, "q.csv", b"a\n1\n").json()
    pending = client.post(url(org_a, "datasets"), {"name": "Pending", "upload_id": up["id"]}, format="json").json()
    resp = client.get(url(org_a, f"datasets/{pending['dataset']['id']}/preview"))
    assert resp.status_code == 409 and resp.json()["code"] == "dataset_not_ready"


# --- tenant isolation -----------------------------------------------------------------------------------
@pytest.mark.ac("P3-AC6")
def test_cross_org_access_is_404(ctx, ready_dataset):
    client, alice, org_a, bob, org_b = ctx
    created, ds = ready_dataset
    dsid = ds["id"]
    client.force_authenticate(bob)
    # bob is not a member of org A
    for path in ("datasets", f"datasets/{dsid}", "data-stats", f"uploads/{ds['current_version']['id']}"):
        assert client.get(url(org_a, path)).status_code == 404
    # through his own org, org A's ids do not exist
    for path in (f"datasets/{dsid}", f"datasets/{dsid}/columns", f"datasets/{dsid}/preview",
                 f"datasets/{dsid}/quality", f"datasets/{dsid}/quality-rules", f"datasets/{dsid}/versions",
                 f"lineage?node_type=dataset&node_id={dsid}", f"jobs/{created['job_id']}"):
        assert client.get(url(org_b, path)).status_code == 404, path
    assert client.patch(url(org_b, f"datasets/{dsid}"), {"name": "x"}, format="json").status_code == 404
    assert client.delete(url(org_b, f"datasets/{dsid}")).status_code == 404
    assert client.post(url(org_b, f"datasets/{dsid}/classification"),
                       {"classification": "public", "attest": True}, format="json").status_code == 404
    assert client.get(url(org_b, "datasets")).json()["items"] == []
    assert client.get(url(org_b, "datasets/not-a-uuid")).status_code == 404
    assert client.get(url(org_b, "data-stats")).json()["datasets"] == 0
    with tenant_context(org_a.id):
        upload_id = Upload.objects.get().id
    up_b = upload_file(client, org_b, "b.csv", b"a\n1\n")
    assert up_b.status_code == 201
    assert client.post(url(org_b, "datasets"), {"name": "steal", "upload_id": str(upload_id)},
                       format="json").status_code == 400
    with tenant_context(org_b.id):
        assert classify_asset(f"dataset:{dsid}") == "confidential"
        assert not Job.objects.filter(id=created["job_id"]).exists()


def test_unauthenticated_is_rejected(api_client, two_orgs):
    _, org_a, _, _ = two_orgs
    # the tenancy mixin resolves membership first: anonymous callers never learn whether the org exists
    assert api_client.get(url(org_a, "datasets")).status_code in (401, 403, 404)
