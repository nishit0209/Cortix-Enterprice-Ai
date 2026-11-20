"""Documents API (DB-backed, nexora_app role with RLS): upload -> processing -> pages/chunks/entities/tables,
hybrid search, policy-gated ask, summaries, classification, delete, permissions and cross-org 404."""

from __future__ import annotations

import io
from uuid import uuid4

import pytest

from apps.ai.policy import check_policy
from apps.ai.services import override_gemini
from apps.ai.testing import FakeTransport, build_engine
from apps.documents.models import Document, DocumentChunk, DocumentEntity, DocumentPage, DocumentTable

from . import synth

pytestmark = pytest.mark.django_db


def _url(world, path: str = "") -> str:
    return f"/api/v1/orgs/{world.slug}/documents{path}"


def _file(name: str, data: bytes):
    f = io.BytesIO(data)
    f.name = name
    return f


@pytest.fixture
def no_ocr(monkeypatch):
    from apps.documents import extraction

    monkeypatch.setattr(extraction, "tesseract_languages", lambda: None)


@pytest.fixture
def ingest(api_client_for, fake_storage, fake_embedder, no_ocr, django_capture_on_commit_callbacks):
    def upload(world, name: str, data: bytes, *, user=None, **fields):
        client = api_client_for(user or world.analyst)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(_url(world), {"file": _file(name, data), **fields}, format="multipart")
        return resp

    return upload


def _ready(world, resp) -> Document:
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["job_id"] and body["status_url"].endswith(body["job_id"]) and body["events_url"].endswith("/events")
    with world.tenant():
        return Document.objects.get(id=body["document"]["id"])


def test_upload_txt_processes_and_exposes_everything(two_orgs, ingest, api_client_for):
    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes(), title="Synthetic quarterly report"))
    assert doc.status == "ready", doc.error
    assert doc.page_count == 2 and doc.chunk_count >= 2 and doc.doc_type == "report"
    assert doc.classification == "internal" and doc.language in ("en", "mixed")
    with a.tenant():
        assert DocumentChunk.objects.filter(document=doc, embedding__isnull=False).count() == doc.chunk_count
        from apps.core.models import Job

        assert Job.objects.get(id=doc.last_job_id).status == "succeeded"
    client = api_client_for(a.viewer)
    detail = client.get(_url(a, f"/{doc.id}")).json()
    assert detail["status"] == "ready" and detail["summary"] and "doc_type_scores" in detail
    pages = client.get(_url(a, f"/{doc.id}/pages")).json()
    assert [p["number"] for p in pages["items"]] == [1, 2] and pages["items"][1]["language"] in ("hi", "mixed")
    chunks = client.get(_url(a, f"/{doc.id}/chunks?limit=1")).json()
    assert len(chunks["items"]) == 1 and chunks["next_cursor"] and chunks["items"][0]["has_embedding"]
    assert client.get(_url(a)).json()["items"][0]["id"] == str(doc.id)


def test_upload_invoice_pdf_entities_and_tables(two_orgs, ingest, api_client_for):
    a = two_orgs.a
    lines = [ln for ln in synth.INVOICE_TEXT.split("\n") if ln]
    pdf = synth.pdf_bytes([lines], table_on=1)
    doc = _ready(a, ingest(a, "invoice.pdf", pdf))
    assert doc.status == "ready" and doc.doc_type == "invoice"
    client = api_client_for(a.viewer)
    ents = client.get(_url(a, f"/{doc.id}/entities")).json()["items"]
    kinds = {e["kind"] for e in ents}
    assert {"email", "gstin", "date", "amount"} <= kinds
    assert all(e["is_pii"] for e in ents if e["kind"] in ("email", "phone"))
    pii = client.get(_url(a, f"/{doc.id}/entities?pii=true")).json()["items"]
    assert pii and all(e["is_pii"] for e in pii)
    tables = client.get(_url(a, f"/{doc.id}/tables")).json()["items"]
    assert tables and tables[0]["header"] == ["Region", "Units", "Revenue"]


@pytest.mark.parametrize(("name", "maker"), [("c.docx", synth.docx_bytes), ("s.xlsx", synth.xlsx_bytes),
                                             ("d.csv", synth.csv_bytes)])
def test_upload_other_formats(two_orgs, ingest, name, maker):
    a = two_orgs.a
    doc = _ready(a, ingest(a, name, maker()))
    assert doc.status == "ready" and doc.chunk_count >= 1
    with a.tenant():
        assert DocumentPage.objects.filter(document=doc).exists()
        if name != "c.docx":
            assert DocumentTable.objects.filter(document=doc).exists()


def test_scanned_pdf_page_without_tesseract(two_orgs, ingest):
    a = two_orgs.a
    pdf = synth.pdf_bytes([["Cover Page", "A synthetic cover page with a text layer."], []], blank_pages=(2,))
    doc = _ready(a, ingest(a, "scan.pdf", pdf))
    assert doc.status == "ready" and doc.ocr_unavailable_pages == 1
    assert any("tesseract" in w for w in doc.warnings)
    with a.tenant():
        assert DocumentPage.objects.get(document=doc, number=2).method == "ocr_unavailable"


def test_corrupt_pdf_marks_document_failed(two_orgs, ingest):
    a = two_orgs.a
    doc = _ready(a, ingest(a, "bad.pdf", b"%PDF-1.4\nnot really a pdf\n%%EOF"))
    assert doc.status == "failed" and doc.error_code in ("document_corrupt", "document_empty")
    with a.tenant():
        from apps.core.models import Job

        assert Job.objects.get(id=doc.last_job_id).status == "failed"


def test_upload_rejections(two_orgs, ingest, settings):
    a = two_orgs.a
    resp = ingest(a, "evil.exe", b"MZ....")
    assert resp.status_code == 422 and resp.json()["code"] == "upload_type_not_allowed"
    resp = ingest(a, "fake.pdf", b"MZ not a pdf")
    assert resp.status_code == 422 and resp.json()["code"] == "upload_content_mismatch"
    settings.DOCUMENT_MAX_UPLOAD_BYTES = 10
    resp = ingest(a, "big.txt", b"x" * 100)
    assert resp.status_code == 413
    with a.tenant():
        assert Document.objects.count() == 0


def test_upload_requires_manage_and_attestation(two_orgs, ingest):
    a = two_orgs.a
    assert ingest(a, "r.txt", synth.txt_bytes(), user=a.viewer).status_code == 403
    resp = ingest(a, "r.txt", synth.txt_bytes(), classification="synthetic")
    assert resp.status_code == 422 and resp.json()["code"] == "attestation_required"
    doc = _ready(a, ingest(a, "r.txt", synth.txt_bytes(), classification="synthetic", attest="true"))
    assert doc.classification == "synthetic" and doc.classification_attested_by == a.analyst.pk


def test_search_hybrid_and_filters(two_orgs, ingest, api_client_for):
    a = two_orgs.a
    rep = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    inv = _ready(a, ingest(a, "invoice.txt", synth.INVOICE_TEXT.encode()))
    client = api_client_for(a.viewer)
    body = client.get(_url(a, "/search?q=North region units sold")).json()
    assert body["retrieval"] == {"vector": True, "fulltext": True, "fusion": "rrf"}
    assert body["results"][0]["document_id"] == str(rep.id)
    assert body["results"][0]["highlights"]
    body = client.get(_url(a, f"/search?q=GSTIN amount due&document_ids={inv.id}")).json()
    assert body["results"] and {r["document_id"] for r in body["results"]} == {str(inv.id)}
    hindi = client.get(_url(a, "/search?q=कृत्रिम परीक्षण")).json()
    assert hindi["results"] and hindi["results"][0]["document_id"] == str(rep.id)
    assert client.get(_url(a, "/search")).status_code == 400
    assert client.get(_url(a, f"/search?q=x&document_ids={uuid4()}")).status_code == 400


def test_ask_real_document_returns_extractive_answer(two_orgs, ingest, api_client_for, settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    engine, parts = build_engine(policy_check=check_policy)  # the REAL gate (org policy + classification)
    client = api_client_for(a.viewer)
    with override_gemini(engine):
        resp = client.post(_url(a, "/ask"), {"question": "How many units did the North region sell?"},
                           format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["mode"] == "extractive" and body["reason"] == "ai_policy_blocked"
    assert body["citations"] and body["citations"][0]["document_id"] == str(doc.id)
    assert {"document_id", "page", "chunk_id", "quote"} <= set(body["citations"][0])
    assert parts["transport"].total_calls == 0


def test_ask_synthetic_document_generative(two_orgs, ingest, api_client_for, settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes(), classification="synthetic", attest="true"))
    transport = FakeTransport().push(FakeTransport.json({
        "answer": "The North region sold 120 units [S1].",
        "citations": [{"source_id": "S1", "quote": "The North region sold 120 units."}],
        "confidence": "high", "unanswerable": False}))
    engine, _ = build_engine(transport=transport, policy_check=check_policy)
    client = api_client_for(a.viewer)
    with override_gemini(engine):
        body = client.post(_url(a, "/ask"), {"question": "How many units did the North region sell?",
                                             "document_ids": [str(doc.id)]}, format="json").json()
    assert body["mode"] == "generative", body
    assert body["citations"][0]["document_id"] == str(doc.id) and body["citations"][0]["chunk_id"]
    assert transport.total_calls == 1


def test_summary_modes(two_orgs, ingest, api_client_for, settings):
    settings.GEMINI_API_KEY = ""
    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    client = api_client_for(a.viewer)
    body = client.get(_url(a, f"/{doc.id}/summary")).json()
    assert body["mode"] == "extractive" and body["sentences"] and body["reason"] is None
    engine, parts = build_engine(policy_check=check_policy)
    with override_gemini(engine):
        body = client.get(_url(a, f"/{doc.id}/summary?mode=generative")).json()
    assert body["mode"] == "extractive" and body["reason"] == "ai_policy_blocked"
    assert parts["transport"].total_calls == 0
    assert client.get(_url(a, f"/{doc.id}/summary?mode=poem")).status_code == 400


def test_classification_update_and_registry(two_orgs, ingest, api_client_for):
    from apps.core.registry import asset_exists, classify_asset

    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    client = api_client_for(a.analyst)
    url = _url(a, f"/{doc.id}/classification")
    assert client.post(url, {"classification": "public"}, format="json").status_code == 422
    resp = client.post(url, {"classification": "public", "attest": True}, format="json")
    assert resp.status_code == 200 and resp.json()["classification"] == "public"
    with a.tenant():
        assert classify_asset(f"document:{doc.id}") == "public"
        assert asset_exists("document", doc.id) is True
        assert classify_asset(f"document:{uuid4()}") == "confidential"
    assert api_client_for(a.viewer).post(url, {"classification": "internal"}, format="json").status_code == 403


def test_patch_reprocess_and_delete(two_orgs, ingest, api_client_for, fake_storage,
                                    django_capture_on_commit_callbacks):
    a = two_orgs.a
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    client = api_client_for(a.analyst)
    assert client.patch(_url(a, f"/{doc.id}"), {"title": "Renamed"}, format="json").json()["title"] == "Renamed"
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(_url(a, f"/{doc.id}/reprocess"), format="json")
    assert resp.status_code == 202
    with a.tenant():
        doc.refresh_from_db()
        assert doc.status == "ready" and DocumentChunk.objects.filter(document=doc).count() == doc.chunk_count
    assert client.delete(_url(a, f"/{doc.id}")).status_code == 204
    with a.tenant():
        assert not Document.objects.filter(id=doc.id).exists()
        assert not DocumentEntity.objects.filter(document_id=doc.id).exists()
    assert doc.storage_key not in fake_storage.objects
    assert client.get(_url(a, f"/{doc.id}")).status_code == 404


def test_cross_org_access_is_404(two_orgs, ingest, api_client_for):
    a, b = two_orgs.a, two_orgs.b
    doc = _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    other = api_client_for(b.owner)
    for path in ("", "/pages", "/chunks", "/entities", "/tables", "/summary"):
        assert other.get(f"/api/v1/orgs/{b.slug}/documents/{doc.id}{path}").status_code == 404
    assert other.delete(f"/api/v1/orgs/{b.slug}/documents/{doc.id}").status_code == 404
    assert other.get(f"/api/v1/orgs/{a.slug}/documents").status_code == 404
    body = other.get(f"/api/v1/orgs/{b.slug}/documents/search?q=North region").json()
    assert body["results"] == []
    resp = other.post(f"/api/v1/orgs/{b.slug}/documents/ask",
                      {"question": "North region units?", "document_ids": [str(doc.id)]}, format="json")
    assert resp.status_code == 400
    assert other.get(f"/api/v1/orgs/{b.slug}/documents").json()["items"] == []


def test_rls_blocks_cross_tenant_rows(two_orgs, ingest):
    a, b = two_orgs.a, two_orgs.b
    _ready(a, ingest(a, "report.txt", synth.txt_bytes()))
    with b.tenant():
        for model in (Document, DocumentPage, DocumentChunk, DocumentEntity, DocumentTable):
            assert model.objects.count() == 0
