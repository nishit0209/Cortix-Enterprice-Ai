"""Upload security (SECURITY_ARCHITECTURE §7, DATA_ENGINEERING_DESIGN §2): size limit, magic bytes, zip bombs,
malformed files, formula injection. Uses testdata/generated/adversarial/*.

HTTP tests run against the as-built upload API (Data Engineering W1): rejection codes are the specific
`upload_*` codes of apps.datasets (CONTRACT "W1 as-built"); object storage is the in-memory fake (prefix still
enforced) and the ingest task runs eagerly when the request's transaction commits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from testing.client import api_client_for, route_exists
from testing.security import FORMULA_TRIGGERS, escape_formula, unescaped_formula_cells

REPO = Path(__file__).resolve().parents[3]
ADV = REPO / "testdata" / "generated" / "adversarial"
GEN = REPO / "testdata" / "generated"
REJECT_STATUSES = (400, 413, 415, 422)
UPLOAD_REJECT_CODES = {
    "upload_rejected",
    "upload_type_not_allowed",
    "upload_content_mismatch",
    "upload_zip_bomb",
    "upload_empty",
    "upload_too_large",
}


def _uploads_url(world) -> str:
    return f"/api/v1/orgs/{world.slug}/uploads"


@pytest.fixture
def uploader(two_orgs, memory_storage, fake_redis):
    a = two_orgs.a
    if not route_exists(_uploads_url(a)):
        pytest.skip("POST /orgs/<org>/uploads not implemented yet (Data Engineering, wave 2)")
    client = api_client_for(a.user("data_engineer"))

    def upload(name: str, data: bytes, content_type: str = "text/csv"):
        return client.post(
            _uploads_url(a), {"file": SimpleUploadedFile(name, data, content_type)}, format="multipart"
        )

    upload.client = client
    upload.world = a
    upload.storage = memory_storage
    return upload


def _assert_rejected(uploader, resp):
    if resp.status_code in (201, 202):  # accepted for asynchronous scanning: the scan must reject it
        body = resp.json()
        status = body.get("status")
        if status != "rejected" and body.get("id"):
            detail = uploader.client.get(f"{_uploads_url(uploader.world)}/{body['id']}")
            status = detail.json().get("status") if detail.status_code == 200 else status
        assert status == "rejected", body
        return
    assert resp.status_code in REJECT_STATUSES, (resp.status_code, resp.content[:300])
    assert resp.json().get("code") in UPLOAD_REJECT_CODES, resp.content[:300]
    assert uploader.storage.keys_for(uploader.world.id) == [], "a rejected upload must not stay in storage"


# ------------------------------------------------------------------------------------------- oracle
def test_formula_oracle():
    assert escape_formula("=1+1") == "'=1+1"
    assert escape_formula("@SUM(A1)") == "'@SUM(A1)"
    assert escape_formula("\t=1") == "'\t=1"
    assert escape_formula("plain") == "plain"
    assert unescaped_formula_cells("a,b\n=1+1,ok\n'=1+1,-10\n") == [(1, 0, "=1+1")]
    assert set(FORMULA_TRIGGERS) == {"=", "+", "-", "@", "\t", "\r"}


# ------------------------------------------------------------------------------------------- uploads
@pytest.mark.django_db
def test_valid_csv_is_accepted(uploader):
    data = (GEN / "sales.csv").read_bytes()
    resp = uploader("sales.csv", data)
    assert resp.status_code == 201, resp.content[:300]
    body = resp.json()
    assert body["filename"] == "sales.csv" and body["size_bytes"] == len(data)
    assert "orgs/" not in resp.content.decode(), "storage keys must not be exposed"


@pytest.mark.django_db
def test_zip_bomb_xlsx_is_rejected(uploader):
    data = (ADV / "zip_bomb.xlsx").read_bytes()
    resp = uploader("report.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    _assert_rejected(uploader, resp)


@pytest.mark.django_db
def test_oversized_upload_is_rejected(uploader, settings):
    settings.MAX_UPLOAD_BYTES = 64 * 1024
    data = b"order_id,region\n" + b"1,North\n" * 20_000  # ~160 KB
    resp = uploader("big.csv", data)
    if resp.status_code in (201, 202):
        _assert_rejected(uploader, resp)
    else:
        assert resp.status_code in (400, 413), resp.status_code
        assert resp.json().get("code") in ("upload_too_large", "upload_rejected", "payload_too_large")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fixture", "name", "content_type"),
    [
        ("png_as.csv", "data.csv", "text/csv"),
        ("mz_as.csv", "data.csv", "text/csv"),
        ("text_as.xlsx", "data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ],
)
def test_wrong_magic_bytes_are_rejected(uploader, fixture, name, content_type):
    _assert_rejected(uploader, uploader(name, (ADV / fixture).read_bytes(), content_type))


@pytest.mark.django_db
@pytest.mark.parametrize("fixture", ["malformed.csv", "huge_column.csv", "wide_columns.csv"])
def test_hostile_csvs_never_cause_server_errors(uploader, fixture):
    resp = uploader(fixture, (ADV / fixture).read_bytes())
    assert resp.status_code < 500, resp.content[:300]


@pytest.mark.django_db
def test_upload_is_not_visible_to_other_orgs(uploader, two_orgs):
    resp = uploader("sales.csv", (GEN / "sales.csv").read_bytes())
    assert resp.status_code == 201
    upload_id = resp.json()["id"]
    b = two_orgs.b
    other = api_client_for(b.owner)
    assert other.get(f"/api/v1/orgs/{b.slug}/uploads/{upload_id}").status_code == 404
    assert other.get(f"/api/v1/orgs/{two_orgs.a.slug}/uploads/{upload_id}").status_code == 404


@pytest.mark.django_db
def test_formula_cells_are_ingested_verbatim_as_text(uploader, django_capture_on_commit_callbacks):
    """Formula payloads must be stored as plain strings (never evaluated or coerced)."""
    a = uploader.world
    datasets_url = f"/api/v1/orgs/{a.slug}/datasets"
    if not route_exists(datasets_url):
        pytest.skip("datasets API not implemented yet (Data Engineering, wave 2)")
    raw = (ADV / "formula_injection.csv").read_bytes()
    up = uploader("formula_injection.csv", raw)
    assert up.status_code == 201
    owner = api_client_for(a.owner)  # datasets.classify: needed for the synthetic attestation
    with django_capture_on_commit_callbacks(execute=True):
        created = owner.post(
            datasets_url,
            {
                "name": "Formula probe",
                "upload_id": up.json()["id"],
                "classification": "synthetic",
                "attest": True,
            },
            format="json",
        )
    assert created.status_code in (201, 202), created.content[:300]
    dataset_id = created.json()["dataset"]["id"]
    detail = owner.get(f"{datasets_url}/{dataset_id}").json()
    assert detail["status"] == "ready", detail.get("last_error")
    preview = owner.get(f"{datasets_url}/{dataset_id}/preview?limit=100")
    assert preview.status_code == 200
    body = preview.json()
    names = [c["name"] for c in body["columns"]]
    notes = [row[names.index("note")] for row in body["rows"]]
    import csv
    import io

    expected = [r["note"] for r in csv.DictReader(io.StringIO(raw.decode("utf-8")))]
    assert [str(n) for n in notes] == expected
