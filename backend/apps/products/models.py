"""Product 360 (API_SPECIFICATION Phase 7, DATABASE_SCHEMA section 10). Every model is a TenantModel with RLS."""

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.ai.vectors import EmbeddingField
from apps.core.tenancy.models import TenantModel

MATCH_STATUSES = [(s, s) for s in ("auto_merged", "pending_review", "confirmed", "rejected")]
CORRECTION_STATUSES = [(s, s) for s in ("proposed", "approved", "rejected", "applied")]


class ProductSettings(TenantModel):
    """Per-org matching configuration (one row per org; defaults in apps.products.matching)."""

    auto_threshold = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.9000"))
    review_threshold = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.7500"))
    weights = models.JSONField(default=dict, blank=True)        # {name, sku, size, price, brand}
    brand_aliases = models.JSONField(default=dict, blank=True)  # folded alias -> canonical brand

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org"], name="product_settings_org_uniq")]


class ProductSource(TenantModel):
    """A dataset mapped to product fields (sku, name, brand, category, price, pack_size, unit, attributes)."""

    STATUS = [(s, s) for s in ("active", "error")]

    name = models.CharField(max_length=200)
    source_system = models.SlugField(max_length=64)
    dataset_id = models.UUIDField()
    field_mapping = models.JSONField(default=dict)
    priority = models.PositiveIntegerField(default=100)  # lower = more trusted (golden values)
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    last_error = models.JSONField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_version_id = models.UUIDField(null=True, blank=True)
    record_count = models.PositiveIntegerField(default=0)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "source_system"], name="product_source_system_uniq")]


class ProductMaster(TenantModel):
    """Golden product of one matched cluster of source records."""

    sku_normalized = models.CharField(max_length=64, blank=True, default="")
    name = models.CharField(max_length=500, blank=True, default="")
    brand = models.CharField(max_length=200, blank=True, default="")
    category = models.CharField(max_length=200, blank=True, default="")
    size = models.CharField(max_length=40, blank=True, default="")
    price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    values = models.JSONField(default=dict)                 # master_values(...) with applied corrections on top
    overrides = models.JSONField(default=dict, blank=True)  # applied corrections {field: value}; survive re-runs
    sources = models.JSONField(default=list)
    record_count = models.PositiveIntegerField(default=0)
    quality = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    search_text = models.TextField(blank=True, default="")
    embedding = EmbeddingField(dim=settings.EMBEDDING_DIM, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "name"], name="product_master_name_idx"),
            models.Index(fields=["org", "sku_normalized"], name="product_master_sku_idx"),
        ]


class ProductSourceRecord(TenantModel):
    source = models.ForeignKey(ProductSource, on_delete=models.CASCADE, related_name="records")
    source_system = models.CharField(max_length=64)
    source_key = models.CharField(max_length=255)
    dataset_id = models.UUIDField(null=True, blank=True)
    attributes = models.JSONField(default=dict)   # mapped raw values
    normalized = models.JSONField(default=dict)   # matching.normalize_record(...)
    master = models.ForeignKey(ProductMaster, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="records")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "source", "source_key"], name="product_record_key_uniq")]


class ProductAttribute(TenantModel):
    ORIGINS = [(s, s) for s in ("extracted", "supplied", "correction")]

    master = models.ForeignKey(ProductMaster, on_delete=models.CASCADE, related_name="attribute_rows")
    key = models.CharField(max_length=80)
    value = models.CharField(max_length=500)
    origin = models.CharField(max_length=16, choices=ORIGINS, default="extracted")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "master", "key"], name="product_attribute_key_uniq")]


class ProductMatch(TenantModel):
    record_a = models.ForeignKey(ProductSourceRecord, on_delete=models.CASCADE, related_name="+")
    record_b = models.ForeignKey(ProductSourceRecord, on_delete=models.CASCADE, related_name="+")
    score = models.DecimalField(max_digits=5, decimal_places=4)
    features = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=MATCH_STATUSES)
    reviewed_by = models.UUIDField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "record_a", "record_b"], name="product_match_pair_uniq")]
        indexes = [models.Index(fields=["org", "status", "-score"], name="product_match_status_idx")]


class ProductCorrection(TenantModel):
    """A proposed change to one master field; applied ONLY when approved (products.review)."""

    ORIGINS = [(s, s) for s in ("rule", "ai", "user")]

    master = models.ForeignKey(ProductMaster, on_delete=models.CASCADE, related_name="corrections")
    field = models.CharField(max_length=80)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField()
    reason = models.CharField(max_length=500, blank=True, default="")
    confidence = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    origin = models.CharField(max_length=8, choices=ORIGINS, default="rule")
    status = models.CharField(max_length=16, choices=CORRECTION_STATUSES, default="proposed")
    proposed_by = models.UUIDField(null=True, blank=True)
    decided_by = models.UUIDField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["org", "status", "-created_at"], name="product_correction_status_idx")]


class ProductQualityScore(TenantModel):
    master = models.ForeignKey(ProductMaster, on_delete=models.CASCADE, related_name="quality_scores")
    completeness = models.DecimalField(max_digits=5, decimal_places=2)
    validity = models.DecimalField(max_digits=5, decimal_places=2)
    consistency = models.DecimalField(max_digits=5, decimal_places=2)
    overall = models.DecimalField(max_digits=5, decimal_places=2)
    issues = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "master"], name="product_quality_master_uniq")]


class ProductRun(TenantModel):
    """One normalize or enrich job (history + concurrency guard)."""

    KINDS = [("normalize", "normalize"), ("enrich", "enrich")]
    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed")]

    kind = models.CharField(max_length=16, choices=KINDS)
    job_id = models.UUIDField(null=True, blank=True)
    requested_by = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS, default="queued")
    options = models.JSONField(default=dict, blank=True)
    stats = models.JSONField(default=dict, blank=True)
    error = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "kind", "-created_at"], name="product_run_idx")]
