"""Document intelligence persistence (AI_ARCHITECTURE.md §7). Every model is a TenantModel with RLS.

Document 1-* DocumentPage (text per page, extraction method / OCR status)
         1-* DocumentChunk (heading-aware chunks, page refs, local e5 embedding + stored tsvector)
         1-* DocumentEntity (regex entities, PII flagged)
         1-* DocumentTable (tables from PDF pages, DOCX, XLSX sheets, CSV; promotable to a dataset)
         1-* ExtractionResult (schema-driven fields with page citations; needs review)
DocumentCollection 1-* Document (optional grouping)
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from apps.ai.vectors import EmbeddingField
from apps.core.tenancy.models import TenantModel

CLASSIFICATIONS = [("synthetic", "synthetic"), ("public", "public"), ("internal", "internal"),
                   ("confidential", "confidential")]
ATTESTED_CLASSIFICATIONS = frozenset({"synthetic", "public"})
STATUSES = [("processing", "processing"), ("ready", "ready"), ("failed", "failed")]
# none: no chunks; complete: every chunk has a vector; pending: some chunks wait for the local embedding model.
EMBEDDING_STATUSES = [("none", "none"), ("complete", "complete"), ("pending", "pending")]
DOC_TYPES = [(t, t) for t in ("invoice", "contract", "resume", "report", "policy", "other")]
PAGE_METHODS = [(m, m) for m in ("text", "ocr", "ocr_unavailable", "ocr_failed", "empty")]
ENTITY_KINDS = [(k, k) for k in ("email", "phone", "date", "amount", "pan", "gstin", "aadhaar", "org")]


EXTRACTION_STATUSES = [(s, s) for s in ("extracted", "needs_review", "approved", "rejected")]
EXTRACTION_METHODS = [("local", "local"), ("gemini", "gemini")]


class DocumentCollection(TenantModel):
    """A named group of documents (e.g. "Supplier invoices FY26")."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    classification = models.CharField(max_length=20, choices=CLASSIFICATIONS, default="internal")
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="doc_collection_name_uniq")]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return self.name


class Document(TenantModel):
    title = models.CharField(max_length=255)
    collection = models.ForeignKey(DocumentCollection, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="documents")
    filename = models.CharField(max_length=255)
    mime = models.CharField(max_length=120)
    file_kind = models.CharField(max_length=10)  # pdf | docx | xlsx | csv | txt
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=512)
    classification = models.CharField(max_length=20, choices=CLASSIFICATIONS, default="internal")
    classification_attested_by = models.UUIDField(null=True, blank=True)
    classification_attested_at = models.DateTimeField(null=True, blank=True)
    doc_type = models.CharField(max_length=20, choices=DOC_TYPES, default="other")
    doc_type_scores = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default="processing")
    page_count = models.IntegerField(default=0)
    language = models.CharField(max_length=10, blank=True, default="")  # en | hi | gu | mixed | und
    languages = models.JSONField(default=dict, blank=True)  # share of letters per language
    error = models.TextField(blank=True, default="")  # failure message (status=failed)
    warnings = models.JSONField(default=list, blank=True)  # non-fatal processing notes
    error_code = models.CharField(max_length=60, blank=True, default="")
    ocr_pages = models.IntegerField(default=0)
    ocr_unavailable_pages = models.IntegerField(default=0)
    chunk_count = models.IntegerField(default=0)
    embedding_status = models.CharField(max_length=10, choices=EMBEDDING_STATUSES, default="none")
    summary = models.TextField(blank=True, default="")  # extractive summary (local)
    uploaded_by = models.UUIDField(null=True, blank=True)
    last_job_id = models.UUIDField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["org", "-created_at"], name="doc_org_time_idx"),
            models.Index(fields=["org", "sha256"], name="doc_org_sha_idx"),
            models.Index(fields=["org", "status"], name="doc_org_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return self.title


class DocumentPage(TenantModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="pages")
    number = models.IntegerField()  # 1-based (sheets / CSV are pages too)
    text = models.TextField(blank=True, default="")
    method = models.CharField(max_length=20, choices=PAGE_METHODS, default="text")
    ocr_confidence = models.FloatField(null=True, blank=True)
    char_count = models.IntegerField(default=0)
    language = models.CharField(max_length=10, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")  # e.g. the sheet name

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["org", "document", "number"], name="doc_page_uniq"),
        ]


class DocumentChunk(TenantModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="chunks")
    ordinal = models.IntegerField()
    text = models.TextField()
    page = models.IntegerField()  # first page of the chunk (citation target)
    page_to = models.IntegerField()
    section = models.CharField(max_length=500, blank=True, default="")
    token_count = models.IntegerField(default=0)
    # vector(dim) with pgvector, double precision[] without it (apps.ai.vectors). HNSW index: migration, pgvector only.
    embedding = EmbeddingField(dim=settings.EMBEDDING_DIM, null=True, blank=True)
    # True while the chunk still needs a vector (the local embedding model was not available when it was
    # processed); the `ai.reembed_pending` job fills it in later. Search uses full-text for such chunks.
    embedding_pending = models.BooleanField(default=False)
    search_vector = SearchVectorField(null=True, blank=True)  # to_tsvector('simple', text)

    class Meta:
        ordering = ["document_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(fields=["org", "document", "ordinal"], name="doc_chunk_uniq"),
        ]
        indexes = [
            models.Index(fields=["org", "document"], name="doc_chunk_pending_idx",
                         condition=models.Q(embedding_pending=True)),
            GinIndex(fields=["search_vector"], name="doc_chunk_fts_gin"),
        ]


class DocumentEntity(TenantModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="entities")
    chunk = models.ForeignKey(DocumentChunk, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    page = models.IntegerField()
    kind = models.CharField(max_length=20, choices=ENTITY_KINDS)
    value = models.CharField(max_length=500)
    normalized = models.CharField(max_length=500, blank=True, default="")
    is_pii = models.BooleanField(default=False)
    confidence = models.FloatField(default=1.0)
    start = models.IntegerField(default=0)  # offset in the page text
    end = models.IntegerField(default=0)

    class Meta:
        ordering = ["page", "start"]
        indexes = [models.Index(fields=["org", "document", "kind"], name="doc_entity_kind_idx")]


class DocumentTable(TenantModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="tables")
    page = models.IntegerField()
    index = models.IntegerField()  # order within the document
    title = models.CharField(max_length=255, blank=True, default="")
    header = models.JSONField(default=list, blank=True)
    rows = models.JSONField(default=list, blank=True)  # list[list[str]] (capped)
    n_rows = models.IntegerField(default=0)
    n_cols = models.IntegerField(default=0)
    truncated = models.BooleanField(default=False)
    promoted_dataset_id = models.UUIDField(null=True, blank=True)  # set by POST document-tables/{id}/promote

    class Meta:
        ordering = ["index"]
        constraints = [
            models.UniqueConstraint(fields=["org", "document", "index"], name="doc_table_uniq"),
        ]


class ExtractionResult(TenantModel):
    """Structured fields extracted from a document for one schema (invoice / receipt / resume).

    `result`: field -> value (null when not found). `field_citations`: field -> [{page, start, end, quote}] or null;
    an uncited field is always null. Results start as `needs_review`; a reviewer approves (optionally correcting
    fields) or rejects them."""

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="extractions")
    schema_key = models.CharField(max_length=40)
    method = models.CharField(max_length=10, choices=EXTRACTION_METHODS, default="local")
    result = models.JSONField(default=dict)
    field_citations = models.JSONField(default=dict)
    field_confidence = models.JSONField(default=dict)
    corrections = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=EXTRACTION_STATUSES, default="needs_review")
    reason = models.CharField(max_length=60, blank=True, default="")  # why the local method was used
    approval_request_id = models.UUIDField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    reviewed_by = models.UUIDField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["org", "document", "-created_at"], name="doc_extraction_doc_idx")]
