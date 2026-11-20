"""W3 documents: search modes when the local embedding model is missing (no database, no model download)."""

from __future__ import annotations

from apps.ai.embeddings import FakeEmbedder, UnavailableEmbedder
from apps.ai.services import override_embedder
from apps.documents import services
from apps.documents.pipeline import embed_chunks
from apps.documents.ranking import Passage


def result(**kw):
    base = {"query": "invoice total", "passages": [], "vector_used": False, "fulltext_used": True,
            "terms": ["invoice", "total"]}
    return services.SearchResult(**{**base, **kw})


def test_modes_and_reasons():
    assert (result(vector_used=True).mode, result(vector_used=True).mode_reason) == ("hybrid", None)
    off = result(vector_reason="embedding_model_unavailable")
    assert (off.mode, off.mode_reason) == ("fulltext", "embedding_model_unavailable")
    pending = result(vector_used=True, pending_chunks=3)
    assert (pending.mode, pending.mode_reason) == ("hybrid", "embeddings_pending")
    assert result(fulltext_used=False, terms=[]).mode == "none"


def test_search_payload_keeps_the_w2_shape_and_adds_the_mode():
    p = Passage(chunk_id="c1", document_id="d1", document_title="Invoice", page=1, page_to=1, section="",
                text="Invoice total INR 1,200", score=0.5)
    body = services.search_payload(result(passages=[p], vector_reason="embedding_model_unavailable"))
    assert body["query"] == "invoice total"
    assert body["results"][0]["chunk_id"] == "c1" and body["results"][0]["highlights"] == [[0, 7], [8, 13]]
    assert body["retrieval"] == {"vector": False, "fulltext": True, "fusion": "rrf", "mode": "fulltext",
                                 "reason": "embedding_model_unavailable", "pending_embeddings": 0}
    assert body["search_mode"] == "fulltext" and body["search_mode_reason"] == "embedding_model_unavailable"
    assert body["search_mode_message"].startswith("Semantic search is unavailable")
    hybrid = services.search_payload(result(vector_used=True))
    assert hybrid["search_mode"] == "hybrid" and hybrid["search_mode_reason"] is None
    assert hybrid["search_mode_message"] is None


def test_vector_ranking_is_skipped_without_the_model():
    # returns before any database access (this test has no database)
    with override_embedder(UnavailableEmbedder()) as emb:
        assert services._vector_ranking("invoice total", None) == (None, "embedding_model_unavailable")
        assert emb.calls == 0


def test_embedding_failures_leave_chunks_pending():
    from apps.documents.chunking import Chunk

    chunks = [Chunk(ordinal=0, text="alpha", page=1, page_to=1, section="", token_count=1)]
    assert embed_chunks(chunks, UnavailableEmbedder()) is None
    assert len(embed_chunks(chunks, FakeEmbedder(dim=8))[0]) == 8
    assert embed_chunks([], UnavailableEmbedder()) == []
