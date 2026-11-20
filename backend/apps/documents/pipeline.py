"""Pure document analysis: Extracted -> pages, chunks, entities, tables, language, type, extractive summary.
No database and no network (embeddings are computed by `embed_chunks` with an injectable embedder)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .chunking import Chunk, chunk_pages
from .classify import classify_document
from .entities import Entity, extract_entities
from .extraction import Extracted, PageContent, TableData
from .ranking import extractive_summary
from .textutil import detect_language, script_shares

log = logging.getLogger("nexora.documents")

MAX_ENTITIES = 5_000
EMBED_BATCH = 32


@dataclass
class PageEntity:
    page: int
    entity: Entity
    chunk_ordinal: int | None = None


@dataclass
class Analysis:
    pages: list[PageContent]
    chunks: list[Chunk]
    entities: list[PageEntity]
    tables: list[tuple[int, TableData]]
    language: str
    languages: dict[str, float]
    doc_type: str
    doc_type_scores: dict[str, float]
    summary: list[dict]
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.method == "ocr")

    @property
    def ocr_unavailable_pages(self) -> int:
        return sum(1 for p in self.pages if p.method == "ocr_unavailable")


def _link_chunk(chunks: list[Chunk], page: int, value: str) -> int | None:
    fallback = None
    for c in chunks:
        if c.page <= page <= c.page_to:
            if value in c.text:
                return c.ordinal
            fallback = fallback if fallback is not None else c.ordinal
    return fallback


def analyze(extracted: Extracted, *, title: str = "") -> Analysis:
    pages = extracted.pages
    full_text = extracted.text
    for p in pages:
        p.language = detect_language(p.text) if p.text else ""
    chunks = chunk_pages(pages)
    entities: list[PageEntity] = []
    for p in pages:
        for e in extract_entities(p.text):
            if len(entities) >= MAX_ENTITIES:
                break
            entities.append(PageEntity(p.number, e, _link_chunk(chunks, p.number, e.value)))
    tables = [(p.number, t) for p in pages for t in p.tables]
    doc_type, scores = classify_document(full_text, title=title)
    warnings = list(extracted.warnings)
    unavailable = sum(1 for p in pages if p.method == "ocr_unavailable")
    if unavailable:
        warnings.append(f"{unavailable} scanned page(s) could not be OCRed (tesseract unavailable)")
    return Analysis(
        pages=pages, chunks=chunks, entities=entities, tables=tables,
        language=detect_language(full_text), languages=script_shares(full_text),
        doc_type=doc_type, doc_type_scores=scores,
        summary=extractive_summary([(p.number, p.text) for p in pages]),
        warnings=warnings,
    )


def embed_chunks(chunks: list[Chunk], embedder) -> list[list[float]] | None:
    """Local passage embeddings (e5 "passage: " prefix applied by the embedder). None when the embedder fails
    (model missing / out of memory): the document stays searchable through full-text search."""
    if not chunks:
        return []
    vectors: list[list[float]] = []
    try:
        for i in range(0, len(chunks), EMBED_BATCH):
            batch = embedder.embed_passages([c.text for c in chunks[i:i + EMBED_BATCH]])
            vectors.extend([float(x) for x in row] for row in batch)
    except Exception as exc:  # noqa: BLE001 - degrade to full-text only, never fail the document
        log.warning("documents: embedding failed (%s); chunks stored without vectors", type(exc).__name__)
        return None
    return vectors
