"""Public service API of apps.documents. Other apps import only from here.

    create_document(fileobj, ...)        -> (Document, Job)   upload + enqueue processing (queue "ml")
    run_processing(org_id, document_id, job_id)              extraction -> analysis -> embeddings -> persist
    hybrid_search(q, document_ids=None, k=8) -> SearchResult  vector cosine top-20 (apps.ai.vectors.nearest:
                                                               pgvector or numpy) + FTS ('simple') top-20, RRF;
                                                               full-text only when the local model is unavailable
    reembed_pending(org_id=None) -> dict                      fill vectors of `embedding_pending` chunks
    ask(question, document_ids=None)    -> dict               policy-gated Gemini answer with verified citations,
                                                               otherwise an extractive answer (no LLM)
    summarize(document, mode)           -> dict               extractive (local) by default; Gemini when allowed
    document_classification(id) / document_exists(id)         registered with apps.core.registry ("document")
    collections, extract/review_extraction, compare, promote_table   W5 Phase 8 rows (implemented in .intel)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.ai.exceptions import AIInvalidOutput, AIPolicyBlocked, AIQuotaExhausted, AIUnavailable
from apps.ai.services import (
    EmbeddingUnavailable,
    embed_query,
    embedding_available,
    gemini,
    get_embedder,
    get_prompt,
    request_trace,
)
from apps.ai.vectors import nearest
from apps.core import audit, jobs, storage
from apps.core.tenancy.context import require_org_id
from apps.core.tenancy.tasks import tenant_step
from nexora.contracts import PayloadClass, PayloadManifest

from . import extraction, filetypes
from .exceptions import (
    AttestationRequired,
    DocumentNotReady,
    DocumentRejected,
    DocumentTooLarge,
    ExtractionError,
    StorageUnavailable,
)
from .models import (
    ATTESTED_CLASSIFICATIONS,
    CLASSIFICATIONS,
    Document,
    DocumentChunk,
    DocumentEntity,
    DocumentPage,
    DocumentTable,
)
from .pipeline import Analysis, analyze, embed_chunks
from .ranking import (
    Passage,
    build_sources,
    extractive_answer,
    extractive_summary,
    rrf_fuse,
    verify_citations,
)
from .textutil import query_terms, term_spans

log = logging.getLogger("nexora.documents")

DEFAULT_MAX_PAGES = 500
VECTOR_TOP_K = 20
FTS_TOP_K = 20
DEFAULT_K = 8
MAX_K = 20
MAX_QUERY_CHARS = 1000
MAX_DOCUMENT_IDS = 50
DEFAULT_VECTOR_MAX_SCAN = 20_000
# Storage errors from the S3 backend (botocore) and the filesystem backend (OSError).
_STORAGE_ERRORS = (BotoCoreError, ClientError, OSError)
PENDING_WARNING = ("semantic embeddings pending: the local embedding model is not available yet "
                   "(run `manage.py download_models`); full-text search works meanwhile")
SEARCH_MODE_MESSAGES = {
    "embedding_model_unavailable": ("Semantic search is unavailable because the local embedding model is not "
                                    "downloaded yet; showing full-text matches only."),
    "embeddings_pending": ("Some passages are still waiting for semantic embeddings; they are matched by "
                           "full-text only for now."),
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
NOT_FOUND = "Not found in the provided documents."

REASON_MESSAGES = {
    "ai_policy_blocked": ("Your organization's AI data policy does not allow sending these documents to Gemini, "
                          "so this is an extractive answer built locally from the most relevant passages. "
                          "Only documents classified synthetic or public can be used for generative answers."),
    "ai_not_configured": "AI is not configured on this server; showing an extractive answer from the documents.",
    "ai_quota_exhausted": "The AI quota is used up for now; showing an extractive answer from the documents.",
    "ai_rate_limited": "AI is busy right now; showing an extractive answer from the documents.",
    "ai_unavailable": "AI is temporarily unavailable; showing an extractive answer from the documents.",
    "ai_limiter_unavailable": "AI is temporarily unavailable; showing an extractive answer from the documents.",
    "ai_invalid_output": "The AI answer could not be validated; showing the most relevant passages instead.",
    "no_valid_citations": ("The AI answer could not be verified against the documents, so it is not shown. "
                           "Here are the most relevant passages."),
    "no_relevant_passages": "No passage in your documents matches this question.",
}


def max_pages() -> int:
    return int(getattr(settings, "DOCUMENT_MAX_PAGES", DEFAULT_MAX_PAGES))


def safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable())[:255]
    return base or "document"


# ------------------------------------------------------------------------------------------- registry hooks
def document_classification(document_id: UUID) -> str:
    value = Document.objects.filter(id=document_id).values_list("classification", flat=True).first()
    return value or "confidential"


def document_exists(document_id: UUID) -> bool:
    return Document.objects.filter(id=document_id).exists()


# ------------------------------------------------------------------------------------------------ lookups
def get_document(document_id) -> Document:
    try:
        doc_uuid = document_id if isinstance(document_id, UUID) else UUID(str(document_id))
        return Document.objects.get(id=doc_uuid)
    except (Document.DoesNotExist, ValueError, TypeError) as exc:
        raise NotFound() from exc


def resolve_document_ids(raw) -> list[UUID] | None:
    """Validate caller-supplied document ids (must exist in this org and be ready). None = all documents."""
    if raw in (None, "", []):
        return None
    if isinstance(raw, str):
        raw = [p for p in raw.split(",") if p.strip()]
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_DOCUMENT_IDS:
        raise ValidationError({"document_ids": [f"Give at most {MAX_DOCUMENT_IDS} document ids."]})
    try:
        ids = list(dict.fromkeys(UUID(str(x).strip()) for x in raw))
    except ValueError as exc:
        raise ValidationError({"document_ids": ["Invalid document id."]}) from exc
    found = dict(Document.objects.filter(id__in=ids).values_list("id", "status"))
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise ValidationError({"document_ids": [f"Unknown document(s): {', '.join(missing)}"]})
    not_ready = [str(i) for i in ids if found[i] != "ready"]
    if not_ready:
        raise DocumentNotReady(f"Document(s) not ready: {', '.join(not_ready)}")
    return ids


# --------------------------------------------------------------------------------------------- upload
def _apply_classification(doc: Document, classification: str, *, attest: bool, user_id) -> None:
    if classification not in dict(CLASSIFICATIONS):
        raise ValidationError({"classification": [f"Must be one of {', '.join(dict(CLASSIFICATIONS))}."]})
    if classification in ATTESTED_CLASSIFICATIONS:
        if attest is not True:
            raise AttestationRequired()
        if user_id is None:
            raise AttestationRequired("An attestation must be made by a user.")
        doc.classification_attested_by = user_id
        doc.classification_attested_at = timezone.now()
    else:
        doc.classification_attested_by = None
        doc.classification_attested_at = None
    doc.classification = classification


def create_document(fileobj, *, title: str = "", classification: str | None = None, attest: bool = False,
                    user_id=None, request=None, collection_id=None):
    size = int(getattr(fileobj, "size", 0) or 0)
    limit = filetypes.max_upload_bytes()
    if size > limit:
        raise DocumentTooLarge(f"The file is {size} bytes; the document limit is {limit} bytes.")
    if size == 0:
        raise DocumentRejected("The file is empty.", code="upload_empty")
    filename = safe_filename(getattr(fileobj, "name", ""))
    raw = getattr(fileobj, "file", fileobj)
    try:
        kind = filetypes.detect_kind(filename, raw)
    except ExtractionError as exc:
        raise DocumentRejected(exc.message, code=exc.code) from exc
    digest = hashlib.sha256()
    raw.seek(0)
    for block in iter(lambda: raw.read(1024 * 1024), b""):
        digest.update(block)
    raw.seek(0)
    doc = Document(
        title=(title or "").strip()[:255] or os.path.splitext(filename)[0][:255] or "Document",
        filename=filename, mime=filetypes.CONTENT_TYPES[kind], file_kind=kind, size_bytes=size,
        sha256=digest.hexdigest(), status="processing", uploaded_by=user_id,
    )
    _apply_classification(doc, classification or "internal", attest=attest, user_id=user_id)
    if collection_id:
        from .intel import get_collection

        try:
            doc.collection = get_collection(collection_id)
        except NotFound as exc:
            raise ValidationError({"collection_id": ["Unknown collection."]}) from exc
    doc.storage_key = storage.org_key("documents", str(doc.id), _SAFE_NAME_RE.sub("_", filename)[:120] or "file")
    try:
        storage.put_object(doc.storage_key, raw, doc.mime)
    except _STORAGE_ERRORS as exc:
        if isinstance(exc, PermissionError):
            raise
        log.warning("documents: upload to object storage failed: %s", type(exc).__name__)
        raise StorageUnavailable() from exc
    job = jobs.create_job("document_process", user_id=user_id, message="Queued")
    doc.last_job_id = job.id
    doc.save()
    audit.record("document.create", target=doc, request=request, metadata={
        "filename": filename, "size_bytes": size, "kind": kind, "sha256": doc.sha256,
        "classification": doc.classification, "attested": doc.classification in ATTESTED_CLASSIFICATIONS,
        "job_id": str(job.id),
    })
    _enqueue(doc, job, user_id=user_id)
    return doc, job


def _enqueue(doc: Document, job, *, user_id) -> None:
    from .tasks import process_document

    kwargs = {"org_id": str(require_org_id()), "document_id": str(doc.id), "job_id": str(job.id),
              "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: process_document.delay(**kwargs))


def reprocess(doc: Document, *, user_id=None, request=None):
    job = jobs.create_job("document_process", user_id=user_id, message="Queued")
    doc.status = "processing"
    doc.error = doc.error_code = ""
    doc.warnings = []
    doc.last_job_id = job.id
    doc.save(update_fields=["status", "error", "error_code", "warnings", "last_job_id", "updated_at"])
    audit.record("document.reprocess", target=doc, request=request, metadata={"job_id": str(job.id)})
    _enqueue(doc, job, user_id=user_id)
    return job


def process_now(doc: Document, *, user_id=None) -> tuple:
    """Re-run processing synchronously (automation `documents.process`: already on a worker). -> (job, result)"""
    job = jobs.create_job("document_process", user_id=user_id, message="Queued")
    doc.status = "processing"
    doc.error = doc.error_code = ""
    doc.last_job_id = job.id
    doc.save(update_fields=["status", "error", "error_code", "last_job_id", "updated_at"])
    audit.record("document.reprocess", target=doc, metadata={"job_id": str(job.id), "via": "automation"})
    return job, run_processing(org_id=require_org_id(), document_id=doc.id, job_id=job.id)


def set_classification(doc: Document, classification: str, *, attest: bool, user_id, request=None) -> Document:
    before = doc.classification
    _apply_classification(doc, classification, attest=attest, user_id=user_id)
    doc.save(update_fields=["classification", "classification_attested_by", "classification_attested_at",
                            "updated_at"])
    audit.record("document.classification.change", target=doc, request=request,
                 changes={"before": {"classification": before}, "after": {"classification": doc.classification}},
                 metadata={"attested": doc.classification in ATTESTED_CLASSIFICATIONS})
    return doc


def update_document(doc: Document, *, title: str | None = None, request=None) -> Document:
    if title is not None and title.strip() and title.strip()[:255] != doc.title:
        before = doc.title
        doc.title = title.strip()[:255]
        doc.save(update_fields=["title", "updated_at"])
        audit.record("document.update", target=doc, request=request,
                     changes={"before": {"title": before}, "after": {"title": doc.title}})
    return doc


def document_download_url(doc: Document, *, expires_s: int = 600) -> str | None:
    """Signed, expiring link to the original upload. The caller has already checked `documents.view`.

    Refuses keys outside the current org's prefix (defence in depth: a row can never point at another org's
    object). Returns None when the key is missing/invalid or storage cannot sign."""
    key = doc.storage_key or ""
    if not key:
        return None
    try:
        if storage.key_org_id(key) != doc.org_id:
            log.warning("documents: %s storage key is outside its org prefix; no download link", doc.id)
            return None
        return storage.presigned_get(key, expires_s=expires_s, filename=doc.filename or None)
    except (*_STORAGE_ERRORS, ValueError):
        log.warning("documents: could not sign a download link for %s", doc.id, exc_info=True)
        return None


def delete_document(doc: Document, *, request=None) -> None:
    audit.record("document.delete", target=doc, request=request,
                 metadata={"filename": doc.filename, "sha256": doc.sha256})
    key = doc.storage_key
    doc.delete()
    try:
        storage.delete_object(key)
    except _STORAGE_ERRORS:
        log.warning("documents: object %s could not be deleted from storage (orphan)", key)


# ------------------------------------------------------------------------------------------ processing
def _fail(org_id, document_id, job_id, code: str, message: str) -> None:
    with tenant_step(org_id):
        Document.objects.filter(id=document_id).update(status="failed", error=message[:2000], error_code=code[:60],
                                                       processed_at=timezone.now())
        jobs.fail(job_id, code=code, message=message)


def persist_analysis(doc: Document, analysis: Analysis, vectors: list[list[float]] | None) -> dict:
    """Replace the document's derived rows (idempotent re-processing). Inside a tenant transaction.

    `vectors=None` means the local embedding model was unavailable: chunks are stored with `embedding_pending=True`
    (full-text searchable now, embedded later by `reembed_pending`)."""
    pending = vectors is None and bool(analysis.chunks)
    DocumentEntity.objects.filter(document=doc).delete()
    DocumentTable.objects.filter(document=doc).delete()
    DocumentChunk.objects.filter(document=doc).delete()
    DocumentPage.objects.filter(document=doc).delete()
    DocumentPage.objects.bulk_create([
        DocumentPage(org_id=doc.org_id, document=doc, number=p.number, text=p.text, method=p.method,
                     ocr_confidence=p.ocr_confidence, char_count=len(p.text), language=p.language,
                     label=p.label[:255])
        for p in analysis.pages
    ], batch_size=500)
    chunk_rows = DocumentChunk.objects.bulk_create([
        DocumentChunk(org_id=doc.org_id, document=doc, ordinal=c.ordinal, text=c.text, page=c.page,
                      page_to=c.page_to, section=c.section, token_count=c.token_count,
                      embedding=(vectors[c.ordinal] if vectors else None), embedding_pending=pending)
        for c in analysis.chunks
    ], batch_size=200)
    DocumentChunk.objects.filter(document=doc).update(search_vector=SearchVector("text", config="simple"))
    by_ordinal = {c.ordinal: c for c in chunk_rows}
    DocumentEntity.objects.bulk_create([
        DocumentEntity(org_id=doc.org_id, document=doc, page=pe.page, kind=pe.entity.kind,
                       value=pe.entity.value[:500], normalized=pe.entity.normalized[:500], is_pii=pe.entity.is_pii,
                       confidence=pe.entity.confidence, start=pe.entity.start, end=pe.entity.end,
                       chunk=by_ordinal.get(pe.chunk_ordinal) if pe.chunk_ordinal is not None else None)
        for pe in analysis.entities
    ], batch_size=500)
    DocumentTable.objects.bulk_create([
        DocumentTable(org_id=doc.org_id, document=doc, page=page, index=i, title=t.title[:255], header=t.header,
                      rows=t.rows, n_rows=t.total_rows, n_cols=len(t.header), truncated=t.truncated)
        for i, (page, t) in enumerate(analysis.tables)
    ], batch_size=200)
    doc.status = "ready"
    doc.page_count = len(analysis.pages)
    doc.language = analysis.language
    doc.languages = analysis.languages
    doc.doc_type = analysis.doc_type
    doc.doc_type_scores = analysis.doc_type_scores
    doc.ocr_pages = analysis.ocr_pages
    doc.ocr_unavailable_pages = analysis.ocr_unavailable_pages
    doc.chunk_count = len(analysis.chunks)
    doc.embedding_status = "none" if not analysis.chunks else ("pending" if pending else "complete")
    doc.summary = " ".join(s["text"] for s in analysis.summary)[:5000]
    doc.warnings = [w[:300] for w in analysis.warnings][:50]
    doc.error = ""
    doc.error_code = ""
    doc.processed_at = timezone.now()
    doc.save()
    return {
        "document_id": str(doc.id), "status": "ready", "pages": doc.page_count, "chunks": doc.chunk_count,
        "entities": len(analysis.entities), "tables": len(analysis.tables), "language": doc.language,
        "doc_type": doc.doc_type, "ocr_pages": doc.ocr_pages, "ocr_unavailable_pages": doc.ocr_unavailable_pages,
        "embedded": vectors is not None, "embedding_status": doc.embedding_status, "warnings": analysis.warnings,
    }


def _notify(user_ids: list, kind: str, title: str, body: str, link: str) -> None:
    """apps.core.notifications.notify (W3, built by Backend in parallel): imported lazily, skipped if missing."""
    if not user_ids:
        return
    try:
        from apps.core.notifications import notify
    except ImportError:
        return
    try:
        with transaction.atomic():  # a savepoint: a failed insert never poisons the processing transaction
            notify(user_ids, kind, title, body, link)
    except Exception:  # noqa: BLE001 - a notification never fails document processing
        log.warning("documents: notification %s failed", kind, exc_info=True)


def document_link(doc_id) -> str:
    return f"/documents/{doc_id}"


def _notify_processed(doc: Document) -> None:
    if not doc.uploaded_by:
        return
    extra = " Semantic search will be enabled once the embedding model is available." \
        if doc.embedding_status == "pending" else ""
    _notify([doc.uploaded_by], "document.processed", f"Document ready: {doc.title}"[:200],
            f"{doc.page_count} page(s), {doc.chunk_count} passage(s) indexed.{extra}", document_link(doc.id))


def _notify_failed(org, document_id) -> None:
    try:
        with tenant_step(org):
            doc = Document.objects.filter(id=document_id).first()
            if doc is None or not doc.uploaded_by:
                return
            _notify([doc.uploaded_by], "document.failed", f"Document processing failed: {doc.title}"[:200],
                    doc.error or "The document could not be processed.", document_link(doc.id))
    except Exception:  # noqa: BLE001
        log.warning("documents: failure notification skipped", exc_info=True)


def _emit_processed(doc: Document, result: dict) -> None:
    """Automation event `document.processed` (metadata only, never document text). A no-op without workflows."""
    try:
        from apps.automation.events import emit
    except ImportError:
        return
    try:
        with transaction.atomic():
            emit("document.processed", {
                "document_id": str(doc.id), "title": doc.title, "doc_type": doc.doc_type,
                "classification": doc.classification, "collection_id": str(doc.collection_id)
                if getattr(doc, "collection_id", None) else None,
                **{k: result.get(k) for k in ("pages", "chunks", "entities", "tables", "language")}})
    except Exception:  # noqa: BLE001 - an automation hiccup never fails document processing
        log.warning("documents: document.processed event not emitted", exc_info=True)


def run_processing(*, org_id, document_id, job_id) -> dict | None:
    """Celery body (queue "ml"). Short tenant transactions around I/O; heavy work runs outside transactions."""
    org = UUID(str(org_id))
    fd, path = tempfile.mkstemp(prefix="nexora-doc-")
    os.close(fd)
    try:
        with tenant_step(org):
            doc = Document.objects.get(id=document_id)
            title, kind, key = doc.title, doc.file_kind, doc.storage_key
            jobs.update(job_id, status="running", progress=5, message="Downloading")
            try:
                storage.download_to(key, path)
            except _STORAGE_ERRORS as exc:
                if isinstance(exc, PermissionError):
                    raise
                raise ExtractionError("storage_unavailable", "The file could not be read from storage.") from exc
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=15, message="Extracting text")
        extracted = extraction.extract(path, kind, max_pages=max_pages())
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=50, message="Analyzing")
        analysis = analyze(extracted, title=title)
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=65, message="Embedding")
        vectors = embed_chunks(analysis.chunks, get_embedder()) if embedding_available() else None
        if vectors is None and analysis.chunks:
            analysis.warnings.append(PENDING_WARNING)
        with tenant_step(org):
            doc = Document.objects.select_for_update().get(id=document_id)
            result = persist_analysis(doc, analysis, vectors)
            audit.record("document.process", target=doc, actor=("system", None),
                         metadata={k: result[k] for k in ("pages", "chunks", "entities", "tables", "doc_type",
                                                          "language", "ocr_pages", "ocr_unavailable_pages")})
            jobs.finish(job_id, result=result)
            _notify_processed(doc)
            _emit_processed(doc, result)
        return result
    except ExtractionError as exc:
        _fail(org, document_id, job_id, exc.code, exc.message)
        _notify_failed(org, document_id)
        return None
    except Document.DoesNotExist:
        log.info("documents: %s deleted before processing", document_id)
        return None
    except Exception as exc:  # noqa: BLE001 - mark failed; the traceback goes to the worker log
        log.exception("documents: processing failed for %s", document_id)
        try:
            _fail(org, document_id, job_id, "document_processing_error",
                  f"Processing failed ({type(exc).__name__}).")
            _notify_failed(org, document_id)
        except DatabaseError:
            log.exception("documents: could not mark %s as failed", document_id)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ------------------------------------------------------------------------------------------------ search
@dataclass
class SearchResult:
    query: str
    passages: list[Passage]
    vector_used: bool
    fulltext_used: bool
    terms: list[str] = field(default_factory=list)
    vector_reason: str | None = None  # why the vector ranking did not run (embedding_model_unavailable)
    pending_chunks: int = 0  # in-scope chunks still waiting for an embedding (full-text only)

    @property
    def mode(self) -> str:
        """hybrid (vector + full-text), fulltext (no semantic ranking) or none (empty query)."""
        if self.vector_used:
            return "hybrid"
        return "fulltext" if (self.fulltext_used or self.vector_reason) else "none"

    @property
    def mode_reason(self) -> str | None:
        if not self.vector_used and self.vector_reason:
            return self.vector_reason
        if self.pending_chunks:
            return "embeddings_pending"
        return None


def _chunk_scope(document_ids: list[UUID] | None):
    qs = DocumentChunk.objects.filter(document__status="ready")
    if document_ids is not None:
        qs = qs.filter(document_id__in=document_ids)
    return qs


def vector_max_scan() -> int:
    return int(getattr(settings, "DOCUMENT_VECTOR_MAX_SCAN", DEFAULT_VECTOR_MAX_SCAN))


def _vector_ranking(q: str, document_ids) -> tuple[list[UUID] | None, str | None]:
    """(ranked chunk ids, None) or (None, reason) when semantic ranking is not possible right now."""
    if not embedding_available():
        return None, "embedding_model_unavailable"
    try:
        qvec = embed_query(q)
    except EmbeddingUnavailable:
        return None, "embedding_model_unavailable"
    except Exception as exc:  # noqa: BLE001 - embedder broken -> full-text only, never a failed request
        log.warning("documents: query embedding failed (%s)", type(exc).__name__)
        return None, "embedding_model_unavailable"
    try:
        with transaction.atomic():
            rows = nearest(_chunk_scope(document_ids), "embedding", qvec, VECTOR_TOP_K, max_scan=vector_max_scan())
    except DatabaseError:
        log.warning("documents: vector ranking failed; full-text only", exc_info=True)
        return None, "vector_search_failed"
    return [r.id for r in rows], None


def _pending_count(document_ids) -> int:
    try:
        return _chunk_scope(document_ids).filter(embedding_pending=True).count()
    except DatabaseError:  # pragma: no cover - informational only
        return 0


def _fulltext_ranking(terms: list[str], document_ids) -> list[UUID]:
    if not terms:
        return []
    query = None
    for t in terms:
        sq = SearchQuery(t, config="simple", search_type="plain")
        query = sq if query is None else query | sq
    rows = (_chunk_scope(document_ids).filter(search_vector=query)
            .annotate(rank=SearchRank(F("search_vector"), query)).order_by("-rank", "id")
            .values_list("id", flat=True)[:FTS_TOP_K])
    return list(rows)


def hybrid_search(q: str, *, document_ids: list[UUID] | None = None, k: int = DEFAULT_K) -> SearchResult:
    q = (q or "").strip()[:MAX_QUERY_CHARS]
    k = max(1, min(int(k), MAX_K))
    terms = query_terms(q)
    vector, vector_reason = _vector_ranking(q, document_ids) if q else (None, None)
    fulltext = _fulltext_ranking(terms, document_ids)
    lists = [lst for lst in (vector, fulltext) if lst]
    fused = rrf_fuse(lists, limit=k) if lists else []
    ids = [cid for cid, _ in fused]
    chunks = {c.id: c for c in DocumentChunk.objects.filter(id__in=ids).select_related("document")}
    passages = [
        Passage(chunk_id=str(cid), document_id=str(chunks[cid].document_id),
                document_title=chunks[cid].document.title, page=chunks[cid].page, page_to=chunks[cid].page_to,
                section=chunks[cid].section, text=chunks[cid].text, score=round(score, 6))
        for cid, score in fused if cid in chunks
    ]
    return SearchResult(q, passages, vector is not None, bool(terms), terms, vector_reason=vector_reason,
                        pending_chunks=_pending_count(document_ids) if q else 0)


def passage_payload(p: Passage, terms: list[str]) -> dict:
    return {"chunk_id": p.chunk_id, "document_id": p.document_id, "document_title": p.document_title,
            "page": p.page, "page_to": p.page_to, "section": p.section, "text": p.text, "score": p.score,
            "highlights": term_spans(p.text, terms)}


def retrieval_payload(result: SearchResult) -> dict:
    return {"vector": result.vector_used, "fulltext": result.fulltext_used, "fusion": "rrf",
            "mode": result.mode, "reason": result.mode_reason, "pending_embeddings": result.pending_chunks}


def search_payload(result: SearchResult) -> dict:
    reason = result.mode_reason
    return {"query": result.query, "results": [passage_payload(p, result.terms) for p in result.passages],
            "search_mode": result.mode, "search_mode_reason": reason,
            "search_mode_message": SEARCH_MODE_MESSAGES.get(reason or ""),
            "retrieval": retrieval_payload(result)}


# --------------------------------------------------------------------------------------------- Q&A
_AI_ERRORS = (AIPolicyBlocked, AIQuotaExhausted, AIUnavailable, AIInvalidOutput)


def _ai_reason(exc: Exception) -> str:
    """Same mapping as nl2sql: policy / invalid output are fixed reasons, quota & availability keep their code."""
    if isinstance(exc, AIPolicyBlocked):
        return "ai_policy_blocked"
    if isinstance(exc, AIInvalidOutput):
        return "ai_invalid_output"
    codes = exc.get_codes() if hasattr(exc, "get_codes") else None
    if isinstance(codes, str) and codes:
        return codes
    return "ai_quota_exhausted" if isinstance(exc, AIQuotaExhausted) else "ai_unavailable"


def _payload_for(sources: list[dict]) -> PayloadManifest:
    refs = tuple(dict.fromkeys(f"document:{s['document_id']}" for s in sources))
    return PayloadManifest({PayloadClass.DOCUMENT_TEXT: refs})


def _extractive_response(question: str, result: SearchResult, reason: str | None, *, ids=()) -> dict:
    answer, citations = extractive_answer(question, result.passages)
    unanswerable = not answer
    return {
        "question": question, "mode": "extractive", "answer": answer or NOT_FOUND, "citations": citations,
        "passages": [passage_payload(p, result.terms) for p in result.passages],
        "confidence": None, "unanswerable": unanswerable, "reason": reason,
        "message": REASON_MESSAGES.get(reason or "", None), "ai_request_ids": [str(i) for i in ids],
        "retrieval": retrieval_payload(result), "search_mode": result.mode,
    }


def ask(question: str, *, document_ids: list[UUID] | None = None, user_id=None) -> dict:
    question = (question or "").strip()
    if not question:
        raise ValidationError({"question": ["A question is required."]})
    if len(question) > MAX_QUERY_CHARS:
        raise ValidationError({"question": [f"At most {MAX_QUERY_CHARS} characters."]})
    result = hybrid_search(question, document_ids=document_ids, k=DEFAULT_K)
    if not result.passages:
        resp = _extractive_response(question, result, "no_relevant_passages")
        resp.update(answer=NOT_FOUND, unanswerable=True, citations=[])
        return resp
    sources = build_sources(result.passages)
    spec = get_prompt("docs.answer")
    with request_trace() as ids:
        try:
            data = gemini().generate_structured(
                feature="documents.ask", prompt_key=spec.key, system=spec.system,
                prompt=spec.render(question=question, sources=sources), schema=spec.response_schema,
                payload=_payload_for(sources), tier=spec.tier, user_id=user_id)
        except _AI_ERRORS as exc:
            return _extractive_response(question, result, _ai_reason(exc), ids=list(ids))
    citations = verify_citations(data.get("citations") or [], sources)
    base = {
        "question": question, "mode": "generative",
        "passages": [passage_payload(p, result.terms) for p in result.passages],
        "ai_request_ids": [str(i) for i in ids],
        "retrieval": retrieval_payload(result), "search_mode": result.mode,
    }
    if data.get("unanswerable"):
        return {**base, "answer": NOT_FOUND, "citations": [], "confidence": data.get("confidence"),
                "unanswerable": True, "reason": None, "message": None}
    if not citations:
        return {**base, "answer": NOT_FOUND, "citations": [], "confidence": None, "unanswerable": True,
                "reason": "no_valid_citations", "message": REASON_MESSAGES["no_valid_citations"]}
    return {**base, "answer": str(data.get("answer") or "").strip(), "citations": citations,
            "confidence": data.get("confidence"), "unanswerable": False, "reason": None, "message": None}


# ------------------------------------------------------------------------------------------- summary
def _extractive_summary_payload(doc: Document, reason: str | None) -> dict:
    pages = list(DocumentPage.objects.filter(document=doc).order_by("number").values_list("number", "text"))
    sentences = extractive_summary(pages)
    return {"document_id": str(doc.id), "mode": "extractive", "summary": " ".join(s["text"] for s in sentences),
            "sentences": sentences, "key_points": [], "reason": reason,
            "message": REASON_MESSAGES.get(reason or "") if reason else None, "ai_request_ids": []}


def summarize(doc: Document, *, mode: str = "extractive", user_id=None) -> dict:
    if doc.status != "ready":
        raise DocumentNotReady()
    if mode not in ("extractive", "generative"):
        raise ValidationError({"mode": ["Use 'extractive' or 'generative'."]})
    if mode == "extractive":
        return _extractive_summary_payload(doc, None)
    chunks = list(DocumentChunk.objects.filter(document=doc).order_by("ordinal")[:60])
    passages = [Passage(chunk_id=str(c.id), document_id=str(doc.id), document_title=doc.title, page=c.page,
                        page_to=c.page_to, section=c.section, text=c.text) for c in chunks]
    sources = build_sources(passages)
    if not sources:
        return _extractive_summary_payload(doc, None)
    spec = get_prompt("docs.summarize")
    with request_trace() as ids:
        try:
            data = gemini().generate_structured(
                feature="documents.summary", prompt_key=spec.key, system=spec.system,
                prompt=spec.render(title=doc.title, sources=sources), schema=spec.response_schema,
                payload=_payload_for(sources), tier=spec.tier, user_id=user_id)
        except _AI_ERRORS as exc:
            out = _extractive_summary_payload(doc, _ai_reason(exc))
            out["ai_request_ids"] = [str(i) for i in ids]
            return out
    return {"document_id": str(doc.id), "mode": "generative", "summary": str(data.get("summary") or "").strip(),
            "sentences": [], "key_points": [str(k) for k in (data.get("key_points") or [])][:8], "reason": None,
            "message": None, "ai_request_ids": [str(i) for i in ids]}


# ------------------------------------------------------------------------------------------- re-embedding
REEMBED_SCAN_SQL = "SELECT DISTINCT org_id FROM documents_documentchunk WHERE embedding_pending"


def reembed_pending(embedder=None, limit: int = 500) -> int:
    """Embed up to `limit` `embedding_pending` chunks of the CURRENT tenant; documents whose chunks are all done
    become `embedding_status="complete"`. Raises EmbeddingUnavailable when the model is (still) missing."""
    from .pipeline import EMBED_BATCH

    embedder = embedder or get_embedder()
    rows = list(DocumentChunk.objects.filter(embedding_pending=True).order_by("document_id", "ordinal")
                .values_list("id", "document_id", "text")[:max(1, int(limit))])
    if not rows:
        return 0
    done = 0
    for i in range(0, len(rows), EMBED_BATCH):
        part = rows[i:i + EMBED_BATCH]
        vectors = embedder.embed_passages([text for _, _, text in part])
        with transaction.atomic():
            for (chunk_id, _doc, _text), vec in zip(part, vectors, strict=True):
                DocumentChunk.objects.filter(id=chunk_id).update(
                    embedding=[float(x) for x in vec], embedding_pending=False)
        done += len(part)
    doc_ids = {doc_id for _, doc_id, _ in rows}
    still = set(DocumentChunk.objects.filter(document_id__in=doc_ids, embedding_pending=True)
                .values_list("document_id", flat=True).distinct())
    complete = [d for d in doc_ids if d not in still]
    if complete:
        Document.objects.filter(id__in=complete, embedding_status="pending").update(
            embedding_status="complete", updated_at=timezone.now())
    log.info("documents: embedded %d pending chunk(s) in %d document(s)", done, len(doc_ids))
    return done


# --------------------------------------------------------------------------------------- W5 re-exports
from .intel import (  # noqa: E402
    compare,
    create_collection,
    delete_collection,
    extract,
    get_collection,
    get_extraction,
    get_table,
    promote_table,
    review_extraction,
    set_document_collection,
    update_collection,
)

__all__ = [
    "ask", "compare", "create_collection", "create_document", "delete_collection", "delete_document", "extract",
    "get_collection", "get_document", "get_extraction", "get_table", "hybrid_search", "process_now", "promote_table",
    "reembed_pending", "reprocess", "review_extraction", "set_classification", "set_document_collection",
    "summarize", "update_collection", "update_document",
]
