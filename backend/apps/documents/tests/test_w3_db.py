"""W3 documents on PostgreSQL (nexora_app role, RLS): processing and search WITHOUT the local embedding model,
re-embedding later, and `apps.ai.vectors` (EmbeddingField + nearest) on whichever vector backend the test database
has (pgvector when the extension is installed, double precision[] otherwise)."""

from __future__ import annotations

import io

import numpy as np
import pytest
from django.db import connection

from apps.ai import vectors
from apps.ai.embeddings import FakeEmbedder, UnavailableEmbedder
from apps.ai.services import override_embedder
from apps.documents import services
from apps.documents.models import Document, DocumentChunk

from . import synth

pytestmark = pytest.mark.django_db

DIM = DocumentChunk._meta.get_field("embedding").dim


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
def upload(api_client_for, fake_storage, no_ocr, django_capture_on_commit_callbacks):
    def _upload(world, name: str, data: bytes, **fields) -> Document:
        client = api_client_for(world.analyst)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(_url(world), {"file": _file(name, data), **fields}, format="multipart")
        assert resp.status_code == 202, resp.content
        with world.tenant():
            return Document.objects.get(id=resp.json()["document"]["id"])

    return _upload


def test_without_the_model_documents_process_and_search_full_text(two_orgs, upload, api_client_for):
    a = two_orgs.a
    with override_embedder(UnavailableEmbedder()):
        doc = upload(a, "report.txt", synth.txt_bytes(), title="Synthetic quarterly report")
        assert doc.status == "ready", doc.error
        assert doc.embedding_status == "pending" and doc.chunk_count >= 2
        assert services.PENDING_WARNING in doc.warnings
        with a.tenant():
            chunks = DocumentChunk.objects.filter(document=doc)
            assert chunks.filter(embedding_pending=True, embedding__isnull=True).count() == doc.chunk_count
        client = api_client_for(a.viewer)
        assert client.get(_url(a, f"/{doc.id}")).json()["embedding_status"] == "pending"
        item = client.get(_url(a, f"/{doc.id}/chunks?limit=1")).json()["items"][0]
        assert item["embedding_pending"] is True and item["has_embedding"] is False
        found = client.get(_url(a, "/search"), {"q": "revenue"}).json()
        assert found["results"], found
        assert found["search_mode"] == "fulltext" and found["search_mode_reason"] == "embedding_model_unavailable"
        assert found["retrieval"]["vector"] is False and found["retrieval"]["pending_embeddings"] == doc.chunk_count
        asked = client.post(_url(a, "/ask"), {"question": "What was the revenue?"}, format="json").json()
        assert asked["mode"] == "extractive" and asked["search_mode"] == "fulltext"

    # the model becomes available: the pending chunks are embedded and search turns hybrid
    with override_embedder(FakeEmbedder()), a.tenant():
        assert services.reembed_pending(FakeEmbedder()) == doc.chunk_count
        doc.refresh_from_db()
        assert doc.embedding_status == "complete"
        assert not DocumentChunk.objects.filter(document=doc, embedding_pending=True).exists()
        assert DocumentChunk.objects.filter(document=doc, embedding__isnull=False).count() == doc.chunk_count
        assert services.reembed_pending(FakeEmbedder()) == 0  # idempotent
    with override_embedder(FakeEmbedder()):
        found = api_client_for(a.viewer).get(_url(a, "/search"), {"q": "revenue"}).json()
        assert found["search_mode"] == "hybrid" and found["retrieval"]["vector"] is True
        assert found["search_mode_reason"] is None


def test_with_the_model_documents_are_complete_immediately(two_orgs, upload):
    a = two_orgs.a
    with override_embedder(FakeEmbedder()):
        doc = upload(a, "report.txt", synth.txt_bytes())
    assert doc.embedding_status == "complete" and services.PENDING_WARNING not in doc.warnings


def _unit(i: int, noise: float = 0.0) -> list[float]:
    v = np.zeros(DIM, dtype=np.float64)
    v[i] = 1.0
    if noise:
        v[(i + 1) % DIM] = noise
    return (v / np.linalg.norm(v)).tolist()


def _doc_with_chunks(vectors_by_ordinal) -> Document:
    pending = any(v is None for v in vectors_by_ordinal)
    doc = Document.objects.create(title="vectors", filename="v.txt", mime="text/plain", file_kind="txt",
                                  size_bytes=1, sha256="0" * 64, storage_key="k", status="ready",
                                  chunk_count=len(vectors_by_ordinal),
                                  embedding_status="pending" if pending else "complete")
    DocumentChunk.objects.bulk_create([
        DocumentChunk(org_id=doc.org_id, document=doc, ordinal=i, text=f"chunk {i}", page=1, page_to=1,
                      embedding=vec, embedding_pending=vec is None)
        for i, vec in enumerate(vectors_by_ordinal)
    ])
    return doc


def test_nearest_ranks_by_cosine_on_the_live_backend(two_orgs):
    a, b = two_orgs.a, two_orgs.b
    with a.tenant():
        doc = _doc_with_chunks([_unit(0), _unit(1), _unit(1, noise=0.5), None])
        other = _doc_with_chunks([_unit(1)])
        qs = DocumentChunk.objects.filter(document=doc)
        hits = vectors.nearest(qs, "embedding", _unit(1), 3)
        assert [h.ordinal for h in hits] == [1, 2, 0]
        assert hits[0].distance == pytest.approx(0.0, abs=1e-5)
        assert hits[1].distance == pytest.approx(1 - 1 / np.sqrt(1.25), abs=1e-5)
        assert hits[2].distance == pytest.approx(1.0, abs=1e-5)
        assert all(h.document_id == doc.id for h in hits)  # the caller's filter is respected
        assert vectors.nearest(qs, "embedding", [0.0] * DIM, 3) == []  # zero query
        loaded = DocumentChunk.objects.get(document=other, ordinal=0).embedding
        assert isinstance(loaded, np.ndarray) and loaded.shape == (DIM,) and loaded[1] == pytest.approx(1.0)
    with b.tenant():  # RLS: the other org sees none of these vectors
        assert vectors.nearest(DocumentChunk.objects.all(), "embedding", _unit(1), 5) == []


def test_array_scan_is_bounded(two_orgs, monkeypatch):
    a = two_orgs.a
    with a.tenant():
        doc = _doc_with_chunks([_unit(0), _unit(1), _unit(2)])
        qs = DocumentChunk.objects.filter(document=doc)
        monkeypatch.setattr(vectors, "field_backend", lambda field, conn: vectors.ARRAY)
        newest_only = vectors.nearest(qs, "embedding", _unit(0), 3, max_scan=1)
        assert len(newest_only) == 1  # at most max_scan candidates are loaded and ranked


def test_embedding_column_and_indexes_match_the_backend():
    kind = vectors.column_type(connection, "documents_documentchunk", "embedding")
    backend = vectors.field_backend(DocumentChunk._meta.get_field("embedding"), connection)
    assert kind in (f"vector({DIM})", "double precision[]")
    assert backend == (vectors.PGVECTOR if kind.startswith("vector") else vectors.ARRAY)
    if vectors.configured_backend() != vectors.AUTO:
        assert backend == vectors.configured_backend()
    with connection.cursor() as cur:
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'documents_documentchunk'")
        names = {r[0] for r in cur.fetchall()}
    assert "doc_chunk_fts_gin" in names  # full-text search is native PostgreSQL on both backends
    assert ("doc_chunk_emb_hnsw" in names) is (backend == vectors.PGVECTOR)


@pytest.mark.django_db(transaction=True, databases=["default", "ops", "durable"])
def test_periodic_job_embeds_pending_chunks_of_every_org(two_orgs):
    from apps.ai.reembed import run_reembed_pending

    for world in (two_orgs.a, two_orgs.b):
        with world.tenant():
            _doc_with_chunks([None, None])
    with override_embedder(FakeEmbedder()):
        out = run_reembed_pending(allow_download=False)
    assert out.get("documents.chunks") == 4
    for world in (two_orgs.a, two_orgs.b):
        with world.tenant():
            assert not DocumentChunk.objects.filter(embedding_pending=True).exists()
            assert set(Document.objects.values_list("embedding_status", flat=True)) == {"complete"}
