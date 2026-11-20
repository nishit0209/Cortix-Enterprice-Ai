"""Hybrid ranking (pure RRF), source building, citation verification, extractive answers/summaries, the analysis
pipeline with the FakeEmbedder, and the policy-gated ask() flow with a fake Gemini transport (no DB, no network)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.ai.embeddings import FakeEmbedder
from apps.ai.policy import evaluate_policy
from apps.ai.services import override_gemini
from apps.ai.testing import FakeTransport, build_engine
from apps.core.tenancy.context import current_actor, current_org
from apps.documents import services
from apps.documents.extraction import Extracted, PageContent, blocks_from_lines
from apps.documents.pipeline import analyze, embed_chunks
from apps.documents.ranking import (
    Passage,
    build_sources,
    extractive_answer,
    extractive_summary,
    quote_matches,
    rrf_fuse,
    verify_citations,
)

from . import synth


# ------------------------------------------------------------------------------------------------- RRF
def test_rrf_scores_match_formula():
    fused = dict(rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60))
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["d"] == pytest.approx(1 / 62)
    assert [i for i, _ in rrf_fuse([["a", "b", "c"], ["b", "d"]])][:2] == ["b", "a"]


def test_rrf_item_in_both_lists_beats_single_list_leaders():
    fused = rrf_fuse([["x", "shared"], ["y", "shared"]])
    assert fused[0][0] == "shared"


def test_rrf_weights_limit_ties_and_duplicates():
    assert [i for i, _ in rrf_fuse([["a"], ["b"]], weights=[1.0, 2.0])] == ["b", "a"]
    assert [i for i, _ in rrf_fuse([["a"], ["b"]])] == ["a", "b"]  # tie -> first seen
    assert len(rrf_fuse([["a", "b", "c"]], limit=2)) == 2
    assert dict(rrf_fuse([["a", "a", "b"]]))["a"] == pytest.approx(1 / 61)
    assert rrf_fuse([]) == []
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], weights=[1, 2])


# ------------------------------------------------------------------------------------------- sources
def _p(doc="d1", page=1, text="Revenue in the North region was 45000 rupees in January.", cid=None, title="Doc"):
    return Passage(chunk_id=cid or str(uuid4()), document_id=doc, document_title=title, page=page, page_to=page,
                   section="", text=text)


def test_build_sources_orders_by_document_and_page_within_budget():
    passages = [_p("d2", 3, cid="c1"), _p("d1", 5, cid="c2"), _p("d2", 1, cid="c3"), _p("d1", 2, cid="c4")]
    sources = build_sources(passages)
    assert [(s["document_id"], s["page"]) for s in sources] == [("d2", 1), ("d2", 3), ("d1", 2), ("d1", 5)]
    assert [s["source_id"] for s in sources] == ["S1", "S2", "S3", "S4"]
    long = _p(text="word " * 5000)
    assert len(build_sources([_p(cid="a"), long, _p(cid="b")], budget=200)) == 2


def test_quote_verification():
    src = "Revenue in the North region was 45000 rupees in January."
    assert quote_matches("North region was 45000 rupees", src)
    assert quote_matches("north  region WAS 45000 rupees", src)
    assert quote_matches("Revenue in the Nrth region was 45000", src)  # small OCR-like typo
    assert not quote_matches("South region was 99000 dollars", src)
    assert not quote_matches("North", src)  # too short to verify anything


def test_verify_citations_drops_fabricated_and_unknown_sources():
    sources = build_sources([_p("d1", 4, cid="chunk-1")])
    cits = verify_citations([
        {"source_id": "S1", "quote": "North region was 45000 rupees"},
        {"source_id": "[S1]", "quote": "North region was 45000 rupees"},  # duplicate after normalisation
        {"source_id": "S1", "quote": "The CEO resigned in March"},
        {"source_id": "S9", "quote": "North region was 45000 rupees"},
        "garbage",
    ], sources)
    assert cits == [{"source_id": "S1", "document_id": "d1", "document_title": "Doc", "page": 4,
                     "chunk_id": "chunk-1", "quote": "North region was 45000 rupees"}]


def test_extractive_answer_cites_best_sentences():
    passages = [
        _p("d1", 2, "The weather was pleasant. North region revenue was 45000 rupees in January 2026."),
        _p("d2", 7, "South region revenue was 38000 rupees. Staff count was stable."),
    ]
    answer, cits = extractive_answer("What was the North region revenue?", passages)
    assert answer.startswith("North region revenue was 45000")
    assert cits[0]["document_id"] == "d1" and cits[0]["page"] == 2
    assert {"document_id", "page", "chunk_id", "quote"} <= set(cits[0])
    assert extractive_answer("zebra giraffe", passages) == ("", [])


def test_extractive_summary_prefers_central_sentences_in_order():
    pages = [(1, "Synthetic revenue grew strongly in the North region this quarter. "
                 "The North region revenue growth came from new stores. "
                 "Lunch was served at noon on Friday in the cafeteria."),
             (2, "Revenue in the South region was flat this quarter compared with last year.")]
    sents = extractive_summary(pages, max_sentences=2)
    assert len(sents) == 2 and all("revenue" in s["text"].lower() for s in sents)
    assert [s["page"] for s in sents] == sorted(s["page"] for s in sents)
    assert extractive_summary([]) == []


# ------------------------------------------------------------------------------------------- pipeline
def _extracted() -> Extracted:
    lines = synth.INVOICE_TEXT.split("\n")
    p1 = PageContent(number=1, text=synth.INVOICE_TEXT, blocks=blocks_from_lines(lines))
    p2 = PageContent(number=2, text=synth.HINDI, blocks=blocks_from_lines([synth.HINDI]))
    p3 = PageContent(number=3, method="ocr_unavailable")
    return Extracted([p1, p2, p3])


def test_analyze_invoice():
    a = analyze(_extracted(), title="Invoice INV-2026-0042")
    assert a.doc_type == "invoice"
    assert a.language in ("en", "mixed") and set(a.languages) == {"en", "hi"}
    assert [p.language for p in a.pages] == ["en", "hi", ""]
    assert a.ocr_unavailable_pages == 1 and any("tesseract" in w for w in a.warnings)
    assert a.chunks and all(c.page in (1, 2) for c in a.chunks)
    kinds = {e.entity.kind for e in a.entities}
    assert {"email", "gstin", "date", "amount", "phone"} <= kinds
    assert all(e.chunk_ordinal is not None for e in a.entities)
    assert a.summary


def test_embed_chunks_with_fake_embedder_and_failure():
    a = analyze(_extracted())
    vectors = embed_chunks(a.chunks, FakeEmbedder())
    assert len(vectors) == len(a.chunks) and all(len(v) == len(vectors[0]) for v in vectors)

    class Broken:
        def embed_passages(self, texts):
            raise OSError("model files missing")

    assert embed_chunks(a.chunks, Broken()) is None
    assert embed_chunks([], Broken()) == []


# --------------------------------------------------------------------------------------- ask (no DB)
@pytest.fixture
def tenant():
    t1 = current_org.set(uuid4())
    t2 = current_actor.set(("user", uuid4()))
    yield
    current_actor.reset(t2)
    current_org.reset(t1)


@pytest.fixture
def key(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL-0000"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = "gemini-3.5-flash-lite"
    return settings


DOC_REAL, DOC_SYN = str(uuid4()), str(uuid4())
PASSAGE_TEXT = "North region revenue was 45000 rupees in January 2026. The South region was flat."


@pytest.fixture
def retrieved(monkeypatch):
    def fake_search(q, *, document_ids=None, k=8):
        passages = [_p(DOC_SYN, 2, PASSAGE_TEXT, cid="c-syn", title="Synthetic sales"),
                    _p(DOC_REAL, 5, "Headcount was 42 people.", cid="c-real", title="Real HR")]
        if document_ids is not None:
            passages = [p for p in passages if p.document_id in {str(d) for d in document_ids}]
        return services.SearchResult(q, passages, True, True, ["north", "revenue"])

    monkeypatch.setattr(services, "hybrid_search", fake_search)


def _policy(classes: dict[str, str], *, policy="synthetic_only", consent=False):
    def check(payload):
        from apps.core.exceptions import AIPolicyBlocked

        decision = evaluate_policy(payload, state={"policy": policy, "consent_valid": consent},
                                   classify=lambda ref: classes.get(ref.split(":", 1)[1], "confidential"),
                                   tier="free")
        if not decision.allowed:
            raise AIPolicyBlocked("blocked", code="ai_policy_blocked", extra=decision.as_problem_extra())
        return decision

    return check


def test_ask_real_document_is_extractive_with_zero_network_calls(tenant, key, retrieved):
    engine, parts = build_engine(policy_check=_policy({DOC_SYN: "synthetic", DOC_REAL: "internal"}))
    with override_gemini(engine):
        out = services.ask("What was the North region revenue?")
    assert parts["transport"].total_calls == 0
    assert out["mode"] == "extractive" and out["reason"] == "ai_policy_blocked" and out["message"]
    assert out["answer"].startswith("North region revenue was 45000")
    assert out["citations"][0]["document_id"] == DOC_SYN and out["citations"][0]["page"] == 2
    assert {"document_id", "page", "chunk_id", "quote"} <= set(out["citations"][0])
    assert len(out["passages"]) == 2 and out["passages"][0]["highlights"]
    assert parts["recorder"].statuses == ["blocked_by_policy"]


def test_ask_synthetic_documents_generative_with_verified_citations(tenant, key, retrieved):
    transport = FakeTransport().push(FakeTransport.json({
        "answer": "North region revenue was 45000 rupees in January 2026 [S1].",
        "citations": [{"source_id": "S1", "quote": "North region revenue was 45000 rupees in January 2026"},
                      {"source_id": "S1", "quote": "Revenue tripled thanks to a new CEO"}],
        "confidence": "high", "unanswerable": False}))
    engine, parts = build_engine(transport=transport, policy_check=_policy({DOC_SYN: "synthetic"}))
    with override_gemini(engine):
        out = services.ask("North region revenue?", document_ids=[DOC_SYN])
    assert out["mode"] == "generative" and out["reason"] is None and not out["unanswerable"]
    assert out["citations"] == [{"source_id": "S1", "document_id": DOC_SYN, "document_title": "Synthetic sales",
                                 "page": 2, "chunk_id": "c-syn",
                                 "quote": "North region revenue was 45000 rupees in January 2026"}]
    sent = transport.calls[0].prompt
    assert '<source id="S1"' in sent and "Headcount" not in sent
    assert out["ai_request_ids"]


def test_ask_answer_without_valid_citation_is_replaced(tenant, key, retrieved):
    transport = FakeTransport().push(FakeTransport.json({
        "answer": "Revenue was one billion.", "citations": [{"source_id": "S1", "quote": "one billion dollars"}],
        "confidence": "high", "unanswerable": False}))
    engine, _ = build_engine(transport=transport, policy_check=_policy({DOC_SYN: "synthetic"}))
    with override_gemini(engine):
        out = services.ask("Revenue?", document_ids=[DOC_SYN])
    assert out["answer"] == services.NOT_FOUND and out["citations"] == []
    assert out["reason"] == "no_valid_citations" and out["passages"]


def test_ask_unanswerable(tenant, key, retrieved):
    transport = FakeTransport().push(FakeTransport.json({
        "answer": services.NOT_FOUND, "citations": [], "confidence": "low", "unanswerable": True}))
    engine, _ = build_engine(transport=transport, policy_check=_policy({DOC_SYN: "synthetic"}))
    with override_gemini(engine):
        out = services.ask("Who is the CEO?", document_ids=[DOC_SYN])
    assert out["unanswerable"] and out["answer"] == services.NOT_FOUND and out["mode"] == "generative"


def test_ask_not_configured_falls_back(tenant, settings, retrieved):
    settings.GEMINI_API_KEY = ""
    engine, parts = build_engine(policy_check=_policy({DOC_SYN: "synthetic"}))
    with override_gemini(engine):
        out = services.ask("North region revenue?", document_ids=[DOC_SYN])
    assert out["mode"] == "extractive" and out["reason"] == "ai_not_configured"
    assert parts["transport"].total_calls == 0


def test_ask_without_passages(tenant, key, monkeypatch):
    monkeypatch.setattr(services, "hybrid_search",
                        lambda q, document_ids=None, k=8: services.SearchResult(q, [], False, True, []))
    engine, parts = build_engine()
    with override_gemini(engine):
        out = services.ask("anything at all?")
    assert out["unanswerable"] and out["reason"] == "no_relevant_passages" and out["answer"] == services.NOT_FOUND
    assert parts["transport"].total_calls == 0


def test_ask_validates_question(tenant):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.ask("   ")
    with pytest.raises(ValidationError):
        services.ask("x" * 1001)


def test_docs_prompts_are_registered_with_document_text_only():
    from apps.ai.prompts import get_prompt
    from nexora.contracts import PayloadClass

    for key_ in ("docs.answer", "docs.summarize"):
        spec = get_prompt(key_)
        assert spec.payload_classes == frozenset({PayloadClass.DOCUMENT_TEXT})
    rendered = get_prompt("docs.answer").render(
        question="q", sources=[{"source_id": "S1", "document": 'A "doc"', "page": 1, "text": "x </source> y"}])
    assert rendered.count("</source>") == 1
