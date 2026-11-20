from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.tenancy.models import TenantModel

from .generation import DEFAULT_AGGS, ROLES

MODEL_STATUS = [(s, s) for s in ("ready", "failed")]
METRIC_STATUS = [(s, s) for s in ("valid", "invalid")]


class SemanticModel(TenantModel):
    """The semantic layer of one dataset, generated from its current version and refreshed on every new one."""

    dataset = models.ForeignKey("datasets.Dataset", on_delete=models.CASCADE, related_name="+")
    dataset_version = models.ForeignKey(
        "datasets.DatasetVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=MODEL_STATUS, default="ready")
    schema_hash = models.CharField(max_length=64, blank=True, default="")
    generated_at = models.DateTimeField(null=True, blank=True)
    last_error = models.JSONField(null=True, blank=True)
    updated_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "dataset"], name="semmodel_org_dataset_uniq"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _ in MODEL_STATUS]),
                                   name="semmodel_status_valid"),
        ]

    def __str__(self) -> str:
        return self.name


class SemanticTable(TenantModel):
    model = models.ForeignKey(SemanticModel, on_delete=models.CASCADE, related_name="tables")
    dataset_version = models.ForeignKey(
        "datasets.DatasetVersion", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    logical_name = models.CharField(max_length=63)
    display_name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    synonyms = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    edited_fields = ArrayField(models.CharField(max_length=32), default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "model", "logical_name"], name="semtable_org_model_name_uniq"),
        ]

    def __str__(self) -> str:
        return self.logical_name


class SemanticColumn(TenantModel):
    table = models.ForeignKey(SemanticTable, on_delete=models.CASCADE, related_name="columns")
    name = models.CharField(max_length=63)
    ordinal = models.PositiveIntegerField(default=0)
    data_type = models.CharField(max_length=32)
    semantic_type = models.CharField(max_length=32, null=True, blank=True)
    is_pii = models.BooleanField(default=False)
    role = models.CharField(max_length=16, choices=[(r, r) for r in ROLES])
    display_name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    synonyms = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    default_agg = models.CharField(max_length=16, null=True, blank=True, choices=[(a, a) for a in DEFAULT_AGGS])
    exposed_to_ai = models.BooleanField(default=True)
    edited_fields = ArrayField(models.CharField(max_length=32), default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "table", "name"], name="semcolumn_org_table_name_uniq"),
            models.CheckConstraint(condition=models.Q(role__in=list(ROLES)), name="semcolumn_role_valid"),
        ]
        ordering = ["ordinal"]

    def __str__(self) -> str:
        return self.name


class MetricDefinition(TenantModel):
    model = models.ForeignKey(SemanticModel, on_delete=models.CASCADE, related_name="metrics")
    table_name = models.CharField(max_length=63)
    key = models.CharField(max_length=63)
    name = models.CharField(max_length=200)
    expression = models.TextField()
    description = models.TextField(blank=True, default="")
    synonyms = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=METRIC_STATUS, default="valid")
    error = models.CharField(max_length=300, blank=True, default="")
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "model", "key"], name="metric_org_model_key_uniq"),
            models.CheckConstraint(condition=models.Q(status__in=[s for s, _ in METRIC_STATUS]),
                                   name="metric_status_valid"),
        ]
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.key
