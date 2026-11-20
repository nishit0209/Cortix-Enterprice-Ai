"""Multi-step API flows shared by integration and security tests (public endpoints only)."""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile


def upload_and_ingest(
    client,
    slug: str,
    filename: str,
    data: bytes,
    capture,
    *,
    name: str = "Dataset",
    classification: str = "synthetic",
    content_type: str = "text/csv",
) -> dict:
    """POST /uploads + POST /datasets (ingest runs eagerly on commit). Returns the final Dataset JSON.

    `capture` is pytest-django's django_capture_on_commit_callbacks fixture. `client` must hold
    datasets.create (+ datasets.classify for synthetic/public, attested here).
    """
    base = f"/api/v1/orgs/{slug}"
    up = client.post(
        f"{base}/uploads", {"file": SimpleUploadedFile(filename, data, content_type)}, format="multipart"
    )
    assert up.status_code == 201, up.content[:500]
    body = {"name": name, "upload_id": up.json()["id"], "classification": classification}
    if classification in ("synthetic", "public"):
        body["attest"] = True
    with capture(execute=True):
        created = client.post(f"{base}/datasets", body, format="json")
    assert created.status_code == 202, created.content[:500]
    dataset = client.get(f"{base}/datasets/{created.json()['dataset']['id']}").json()
    assert dataset["status"] == "ready", dataset.get("last_error")
    return dataset


def job_outcome(client, slug: str, job_id) -> dict:
    """GET /jobs/{id} -> {status, error, result}."""
    resp = client.get(f"/api/v1/orgs/{slug}/jobs/{job_id}")
    assert resp.status_code == 200, resp.content[:300]
    return resp.json()


def items(resp) -> list:
    """List payload of a response: the `items` envelope or a bare array."""
    body = resp.json()
    return body["items"] if isinstance(body, dict) and "items" in body else body
