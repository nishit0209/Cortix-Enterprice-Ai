"""W4 documents: `download_url` on the document detail (signed via apps.core.storage.presigned_get)."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest

from apps.core import storage
from apps.documents import services
from apps.documents.models import Document
from apps.documents.serializers import DOWNLOAD_URL_TTL_S, DocumentDetailSerializer

from . import synth


@pytest.fixture
def fs_storage(settings, tmp_path):
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = str(tmp_path)
    settings.STORAGE_URL_SIGNING_KEY = "synthetic-signing-key-for-tests"
    return tmp_path


def _doc(org_id, key=None, filename="report.pdf") -> Document:
    key = key if key is not None else f"orgs/{org_id}/documents/{uuid4()}/{filename}"
    return Document(id=uuid4(), org_id=org_id, filename=filename, storage_key=key, title="Report")


def test_download_url_is_a_signed_link_to_the_documents_own_key(fs_storage):
    org = uuid4()
    doc = _doc(org)
    with storage.org_scope(org):
        url = services.document_download_url(doc)
    assert url and url.startswith("/api/v1/files/")
    key, filename = storage.verify_download(url.rsplit("/", 1)[-1])
    assert (key, filename) == (doc.storage_key, "report.pdf")


def test_a_key_outside_the_org_prefix_gets_no_link(fs_storage):
    org = uuid4()
    with storage.org_scope(org):
        assert services.document_download_url(_doc(org, key=f"orgs/{uuid4()}/documents/x/report.pdf")) is None
        assert services.document_download_url(_doc(org, key="")) is None
        assert services.document_download_url(_doc(org, key="not-an-org-key/x.pdf")) is None


def test_detail_serializer_exposes_download_url_and_ttl(fs_storage):
    org = uuid4()
    doc = _doc(org)
    with storage.org_scope(org):
        data = DocumentDetailSerializer(doc).data
    assert data["download_url"].startswith("/api/v1/files/")
    assert data["download_url_expires_in"] == DOWNLOAD_URL_TTL_S


@pytest.mark.django_db
def test_detail_route_returns_a_working_download_url(two_orgs, api_client_for, fs_storage, monkeypatch,
                                                     django_capture_on_commit_callbacks):
    from apps.documents import extraction

    monkeypatch.setattr(extraction, "tesseract_languages", lambda: None)
    a, b = two_orgs
    client = api_client_for(a.user("data_engineer"))
    f = io.BytesIO(synth.txt_bytes())
    f.name = "notes.txt"
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(f"/api/v1/orgs/{a.slug}/documents", {"file": f}, format="multipart")
    assert resp.status_code == 202, resp.content
    doc_id = resp.json()["document"]["id"]
    detail = api_client_for(a.viewer).get(f"/api/v1/orgs/{a.slug}/documents/{doc_id}")
    assert detail.status_code == 200
    url = detail.json()["download_url"]
    assert url.startswith("/api/v1/files/")
    download = api_client_for(a.viewer).get(url)
    assert download.status_code == 200
    # another org never sees the document, so never gets a link
    assert api_client_for(b.owner).get(f"/api/v1/orgs/{b.slug}/documents/{doc_id}").status_code == 404
