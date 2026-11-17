from django.db import models

from apps.core.tenancy.models import TenantModel

RULE_TYPES = [
    (t, t)
    for t in (
        "not_null", "unique", "range", "regex", "allowed_values", "type", "freshness", "row_count", "referential",
        "expression",
    )
]
SEVERITIES = [("info", "info"), ("warn", "warn"), ("error", "error")]
ORIGINS = [("manual", "manual"), ("suggested_profile", "suggested_profile"), ("suggested_ai", "suggested_ai")]


class QualityRule(TenantModel):
    dataset = models.ForeignKey("datasets.Dataset", on_delete=models.CASCADE, related_name="quality_rules")
    column_name = models.CharField(max_length=63, null=True, blank=True)
    rule_type = models.CharField(max_length=20, choices=RULE_TYPES)
    params = models.JSONField(default=dict, blank=True)
    severity = models.CharField(max_length=8, choices=SEVERITIES, default="warn")
    is_blocking = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    origin = models.CharField(max_length=20, choices=ORIGINS, default="manual")
    description = models.CharField(max_length=300, blank=True, default="")
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(rule_type__in=[t for t, _ in RULE_TYPES]), name="qrule_type_valid"),
            models.CheckConstraint(condition=models.Q(severity__in=[s for s, _ in SEVERITIES]), name="qrule_severity_valid"),
        ]
        indexes = [models.Index(fields=["org", "dataset"], name="qrule_org_ds_idx")]
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.rule_type}({self.column_name or '*'})"


class QualityResult(TenantModel):
    rule = models.ForeignKey(QualityRule, on_delete=models.CASCADE, related_name="results")
    dataset_version = models.ForeignKey("datasets.DatasetVersion", on_delete=models.CASCADE, related_name="+")
    run_id = models.UUIDField(null=True, blank=True)
    passed = models.BooleanField()
    evaluated_count = models.BigIntegerField(default=0)
    failed_count = models.BigIntegerField(default=0)
    sample_failures = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")
    measured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "dataset_version", "-measured_at"], name="qresult_org_ver_idx"),
            models.Index(fields=["org", "rule", "-measured_at"], name="qresult_org_rule_idx"),
        ]


class QualityReport(TenantModel):
    dataset_version = models.ForeignKey("datasets.DatasetVersion", on_delete=models.CASCADE, related_name="+")
    score = models.DecimalField(max_digits=5, decimal_places=2)
    dimensions = models.JSONField(default=dict)
    duplicates = models.JSONField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "dataset_version"], name="qreport_org_version_uniq"),
        ]
