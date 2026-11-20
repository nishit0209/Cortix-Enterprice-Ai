"""NL->SQL question cache (exact + semantic). Stores PRE-policy SQL templates over logical table names;
the guard applies the caller's policy at execution time."""

from __future__ import annotations

from django.db import models

from apps.ai.vectors import EmbeddingField
from apps.core.tenancy.models import TenantModel

EMBEDDING_DIM = 768  # intfloat/multilingual-e5-base; changing it needs a migration + re-embed job


class BiQueryCache(TenantModel):
    SOURCES = [("builder", "builder"), ("llm", "llm"), ("verified", "verified"), ("manual", "manual")]

    dataset_id = models.UUIDField()
    dataset_version_id = models.UUIDField(null=True, blank=True)
    # dataset_version_id when known, else dataset_id: the cache scope used for uniqueness.
    scope_id = models.UUIDField()
    question_text = models.TextField()
    normalized_question = models.TextField()
    question_hash = models.CharField(max_length=64)
    template_question = models.TextField()
    slot_shape = models.CharField(max_length=20, default="n0p0")
    slot_values = models.JSONField(default=dict, blank=True)
    sql_template = models.TextField()
    template_slots = models.JSONField(default=list, blank=True)
    slot_equalities = models.JSONField(default=list, blank=True)
    tables = models.JSONField(default=list, blank=True)
    columns = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True, default="")
    assumptions = models.JSONField(default=list, blank=True)
    chart_hint = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=SOURCES)
    verified = models.BooleanField(default=False)
    verified_by = models.UUIDField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    # vector(768) with pgvector, double precision[] without it (apps.ai.vectors); the HNSW index is created by
    # the migration only on pgvector.
    embedding = EmbeddingField(dim=EMBEDDING_DIM, null=True, blank=True)
    hit_count = models.IntegerField(default=0)
    last_hit_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "scope_id", "question_hash"], name="nl2sql_cache_scope_q_uniq"),
        ]
        indexes = [
            models.Index(fields=["org", "scope_id", "slot_shape"], name="nl2sql_cache_scope_idx"),
        ]
