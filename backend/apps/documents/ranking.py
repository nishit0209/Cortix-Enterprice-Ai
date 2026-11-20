"""Pure retrieval / answering helpers (no DB, no network):

* rrf_fuse            — reciprocal rank fusion of ranked id lists (vector top-k + full-text top-k)
* build_sources       — order passages by document/page and fit them into a token budget as S1..Sn
* verify_citations    — keep only citations whose quote fuzzy-matches the cited source (partial ratio >= 90)
* extractive_answer   — best query-matching sentences from the top passages, each with a citation
* extractive_summary  — local sentence-scoring summary (term frequency + position, no AI)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from .textutil import STOPWORDS, WORD_RE, estimate_tokens, prose_sentences, query_terms

RRF_K = 60
QUOTE_MIN_SCORE = 90.0
CONTEXT_TOKEN_BUDGET = 6000
MIN_QUOTE_CHARS = 8


def rrf_fuse(ranked_lists: Sequence[Sequence[Hashable]], *, k: int = RRF_K,
             weights: Sequence[float] | None = None, limit: int | None = None) -> list[tuple[Hashable, float]]:
    """score(d) = sum_i w_i / (k + rank_i(d)), rank 1-based. Ties keep first-seen order (stable)."""
    weights = list(weights) if weights is not None else [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights must match ranked_lists")
    scores: dict[Hashable, float] = {}
    first_seen: dict[Hashable, int] = {}
    order = 0
    for lst, w in zip(ranked_lists, weights, strict=True):
        seen_here: set = set()
        for rank, item in enumerate(lst, start=1):
            if item in seen_here:
                continue
            seen_here.add(item)
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    fused = sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
    return fused[:limit] if limit is not None else fused


@dataclass(frozen=True)
class Passage:
    chunk_id: str
    document_id: str
    document_title: str
    page: int
    page_to: int
    section: str
    text: str
    score: float = 0.0


def build_sources(passages: Sequence[Passage], *, budget: int = CONTEXT_TOKEN_BUDGET) -> list[dict]:
    """Keep passages in relevance order until the budget is used, then present them ordered by document and
    page with ids S1..Sn (AI_ARCHITECTURE §7.5)."""
    kept: list[Passage] = []
    used = 0
    for p in passages:
        tokens = estimate_tokens(p.text)
        if kept and used + tokens > budget:
            continue
        kept.append(p)
        used += tokens
    doc_order: dict[str, int] = {}
    for p in kept:
        doc_order.setdefault(p.document_id, len(doc_order))
    kept.sort(key=lambda p: (doc_order[p.document_id], p.page, p.chunk_id))
    return [{"source_id": f"S{i}", "chunk_id": p.chunk_id, "document_id": p.document_id,
             "document": p.document_title, "page": p.page, "text": p.text} for i, p in enumerate(kept, start=1)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().strip('"“”\'')).lower()


def quote_matches(quote: str, source_text: str, *, threshold: float = QUOTE_MIN_SCORE) -> bool:
    q, s = _norm(quote), _norm(source_text)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    if q in s:
        return True
    return fuzz.partial_ratio(q, s) >= threshold


def verify_citations(citations: Sequence[dict], sources: Sequence[dict]) -> list[dict]:
    """Map model citations {source_id, quote} to verified [{document_id, page, chunk_id, quote, source_id}]."""
    by_id = {s["source_id"]: s for s in sources}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        sid = str(c.get("source_id", "")).strip().strip("[]")
        quote = str(c.get("quote", "")).strip()
        src = by_id.get(sid)
        if src is None or not quote_matches(quote, src["text"]):
            continue
        key = (sid, _norm(quote))
        if key in seen:
            continue
        seen.add(key)
        out.append({"source_id": sid, "document_id": src["document_id"], "document_title": src["document"],
                    "page": src["page"], "chunk_id": src["chunk_id"], "quote": quote[:600]})
    return out


def _sentence_score(sentence: str, terms: set[str]) -> float:
    words = [w.lower() for w in WORD_RE.findall(sentence)]
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in terms)
    distinct = len(terms & set(words))
    return distinct * 2.0 + hits / math.sqrt(len(words))


def extractive_answer(question: str, passages: Sequence[Passage], *, max_sentences: int = 3) -> tuple[str, list[dict]]:
    """Answer text built from the best-matching sentences of the top passages + one citation per sentence."""
    terms = set(query_terms(question))
    candidates: list[tuple[float, int, str, Passage]] = []
    for rank, p in enumerate(passages[:5]):
        for sentence in prose_sentences(p.text):
            if len(sentence) < 20:
                continue
            score = _sentence_score(sentence, terms)
            if score > 0:
                candidates.append((score - rank * 0.25, rank, sentence, p))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    chosen: list[tuple[str, Passage]] = []
    for _score, _rank, sentence, p in candidates:
        if any(fuzz.ratio(sentence, s) > 90 for s, _ in chosen):
            continue
        chosen.append((sentence, p))
        if len(chosen) >= max_sentences:
            break
    citations = [{"source_id": None, "document_id": p.document_id, "document_title": p.document_title,
                  "page": p.page, "chunk_id": p.chunk_id, "quote": s[:600]} for s, p in chosen]
    return " ".join(s for s, _ in chosen), citations


def extractive_summary(pages: Sequence[tuple[int, str]], *, max_sentences: int = 5,
                       max_candidates: int = 4000) -> list[dict]:
    """Classic frequency summarizer: sentence score = mean normalised content-word frequency, with a bonus for
    early sentences and a penalty for very short/long ones; the top sentences are returned in document order."""
    sentences: list[tuple[int, int, str]] = []
    for page, text in pages:
        for s in prose_sentences(text or ""):
            if 30 <= len(s) <= 600:
                sentences.append((len(sentences), page, s))
            if len(sentences) >= max_candidates:
                break
    if not sentences:
        return []
    freq: Counter[str] = Counter()
    for _, _, s in sentences:
        freq.update(w.lower() for w in WORD_RE.findall(s) if w.lower() not in STOPWORDS and len(w) > 2
                    and not w.isdigit())
    if not freq:
        return [{"text": s, "page": p} for _, p, s in sentences[:max_sentences]]
    top = freq.most_common(1)[0][1]
    n = len(sentences)
    scored = []
    for idx, page, s in sentences:
        words = [w.lower() for w in WORD_RE.findall(s)]
        content = [w for w in words if w in freq]
        if not content:
            continue
        base = sum(freq[w] / top for w in content) / math.sqrt(len(words))
        position = 1.0 + 0.5 * (1 - idx / max(1, n - 1)) if n > 1 else 1.5
        scored.append((base * position, idx, page, s))
    scored.sort(key=lambda t: -t[0])
    chosen: list[tuple[int, int, str]] = []
    for _score, idx, page, s in scored:
        if any(fuzz.ratio(s, c) > 85 for _, _, c in chosen):
            continue
        chosen.append((idx, page, s))
        if len(chosen) >= max_sentences:
            break
    chosen.sort()
    return [{"text": s, "page": page} for _, page, s in chosen]
