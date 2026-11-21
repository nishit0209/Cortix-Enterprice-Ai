from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.tenancy.models import TenantModel

CLASSIFICATIONS = [
    ("synthetic", "synthetic"),
    ("public", "public"),
    ("internal", "internal"),
    ("confidential", "confidential"),
]
ATTESTED_CLASSIFICATIONS = ("synthetic", "public")


class Upload(TenantModel):
    """A raw file streamed to object storage (orgs/<org>/uploads/<id>/<filename>)."""

    STATUS = [(s, s) for s in ("uploaded", "scanning", "ready", "rejected")]
    KINDS = [(k, k) for k in ("csv", "tsv", "txt", "xlsx", "xls", "json", "ndjson")]

    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    file_kind = models.CharField(max_length=10, choices=KINDS)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    object_key = models.CharField(max_length=400)
    status = models.CharField(max_length=16, choices=STATUS, default="uploaded")
    reject_reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "-created_at"], name="upload_org_time_idx"),
            models.Index(fields=["org", "sha256"], name="upload_org_sha_idx"),
        ]

    def __str__(self) -> str:
        return self.filename


class Dataset(TenantModel):
    STATUS = [(s, s) for s in ("processing", "ready", "failed", "archived", "deleted")]
    # "derived": written by publish_frame (ML batch predictions, Customer 360 / Product 360 exports)
    SOURCE_KINDS = [(s, s) for s in ("upload", "connector", "pipeline", "derived")]

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220)
    description = models.TextField(blank=True, default="")
    source_kind = models.CharField(max_length=16, choices=SOURCE_KINDS, default="upload")
    data_source_id = models.UUIDField(null=True, blank=True)  # connectors.DataSource (wave 2)
    classification = models.CharField(max_length=16, choices=CLASSIFICATIONS, default="internal")
    classification_attested_by = models.UUIDField(null=True, blank=True)
    classification_attested_at = models.DateTimeField(null=True, blank=True)
    owner_id = models.UUIDField(null=True, blank=True)
    current_version = models.ForeignKey(
        "DatasetVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=16, choices=STATUS, default="processing")
    tags = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    row_count = models.BigIntegerField(null=True, blank=True)
    column_count = models.IntegerField(null=True, blank=True)
    quality_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    last_error = models.JSONField(null=True, blank=True)
    last_job_id = models.UUIDField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "slug"], name="dataset_org_slug_uniq"),
            models.CheckConstraint(
                condition=models.Q(classification__in=[c for c, _ in CLASSIFICATIONS]),
                name="dataset_classification_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(classification__in=ATTESTED_CLASSIFICATIONS)
                | models.Q(classification_attested_at__isnull=False, classification_attested_by__isnull=False),
                name="dataset_attestation_required",
            ),
        ]
        indexes = [
            models.Index(fields=["org", "-created_at"], name="dataset_org_time_idx"),
            models.Index(fields=["org", "status"], name="dataset_org_status_idx"),
        ]

    def __str__(self) -> str:
        return self.name


class DatasetVersion(TenantModel):
    """One immutable table `<wh_schema>.<wh_table>` in nexora_wh."""

    STATUS = [(s, s) for s in ("loading", "ready", "failed", "retired")]

    dataset = models.ForeignKey(Dataset, on_delete=models.RESTRICT, related_name="versions")
    version = models.PositiveIntegerField()
    wh_schema = models.CharField(max_length=63, blank=True, default="")
    wh_table = models.CharField(max_length=63, blank=True, default="")
    row_count = models.BigIntegerField(null=True, blank=True)
    byte_size = models.BigIntegerField(null=True, blank=True)
    schema_hash = models.CharField(max_length=64, blank=True, default="")
    upload = models.ForeignKey(Upload, null=True, blank=True, on_delete=models.RESTRICT, related_name="versions")
    created_by_run_id = models.UUIDField(null=True, blank=True)
    job_id = models.UUIDField(null=True, blank=True)
    profile = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUS, default="loading")
    error = models.JSONField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "dataset", "version"], name="dsversion_org_ds_ver_uniq"),
        ]
        indexes = [models.Index(fields=["org", "dataset", "-version"], name="dsversion_org_ds_idx")]
        ordering = ["-version"]

    def __str__(self) -> str:
        return f"{self.dataset_id} v{self.version}"


class DatasetColumn(TenantModel):
    version = models.ForeignKey(DatasetVersion, on_delete=models.CASCADE, related_name="columns")
    ordinal = models.PositiveIntegerField()
    name = models.CharField(max_length=63)
    source_name = models.CharField(max_length=500, blank=True, default="")
    data_type = models.CharField(max_length=32)
    semantic_type = models.CharField(max_length=32, null=True, blank=True)
    nullable = models.BooleanField(default=True)
    is_pii = models.BooleanField(default=False)
    pii_type = models.CharField(max_length=32, null=True, blank=True)
    pii_source = models.CharField(max_length=32, null=True, blank=True)
    stats = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "version", "name"], name="dscolumn_org_ver_name_uniq"),
        ]
        indexes = [models.Index(fields=["org", "version", "ordinal"], name="dscolumn_org_ver_idx")]
        ordering = ["ordinal"]

    def __str__(self) -> str:
        return self.name
