"""ML workspace data model (ML_ARCHITECTURE §1-§9). Every model is a TenantModel with RLS (migration 0001).

    MLProject ─┬─ DatasetSnapshot (frozen dataset version + policy-filtered columns, parquet in storage)
               ├─ Experiment (one algorithm run on a snapshot: metrics, importances, SHAP, artifact)
               └─ MLModel ── ModelVersion (registered artifact, feature schema, stage)
                               ├─ PredictionLog (append-only: counts/hashes, never raw rows)
                               └─ DriftReport (append-only)
"""

from __future__ import annotations

from django.db import models

from apps.core.tenancy.models import TenantModel

TASK_TYPES = [(t, t) for t in ("classification", "regression", "forecast", "clustering")]
EXPERIMENT_STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed")]
SNAPSHOT_STATUS = [(s, s) for s in ("building", "ready", "failed")]
EDA_STATUS = [(s, s) for s in ("none", "running", "ready", "failed")]
STAGES = [(s, s) for s in ("none", "staging", "production", "archived")]
PREDICTION_SOURCES = [(s, s) for s in ("api", "ui", "batch")]
CALLER_TYPES = [(s, s) for s in ("user", "api_key", "system")]
DRIFT_STATUS = [(s, s) for s in ("ok", "warning", "alert", "insufficient_data")]


class MLProject(TenantModel):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    business_goal = models.TextField(blank=True, default="")
    dataset_id = models.UUIDField()
    target = models.CharField(max_length=255, blank=True, default="")
    task_type = models.CharField(max_length=16, choices=TASK_TYPES)
    task_type_source = models.CharField(max_length=8, default="auto")  # auto | user
    time_column = models.CharField(max_length=255, blank=True, default="")
    features = models.JSONField(default=list)
    column_kinds = models.JSONField(default=dict)
    excluded_by_policy = models.JSONField(default=list)
    leakage = models.JSONField(default=list)  # [{column, reason, message, corr?}]
    primary_metric = models.CharField(max_length=40, blank=True, default="")
    profile = models.JSONField(default=dict)  # creation-time column profile (aggregates only)
    eda = models.JSONField(null=True, blank=True)
    eda_status = models.CharField(max_length=10, choices=EDA_STATUS, default="none")
    eda_job_id = models.UUIDField(null=True, blank=True)
    eda_narrative = models.TextField(blank=True, default="")
    eda_narrative_source = models.CharField(max_length=16, blank=True, default="")  # template | gemini
    status = models.CharField(max_length=10, default="active")  # active | archived
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "-created_at"], name="mlproject_org_created_idx")]


class DatasetSnapshot(TenantModel):
    project = models.ForeignKey(MLProject, on_delete=models.CASCADE, related_name="snapshots")
    dataset_id = models.UUIDField()
    dataset_version_id = models.UUIDField(null=True, blank=True)
    dataset_version = models.PositiveIntegerField(null=True, blank=True)
    columns = models.JSONField(default=list)  # columns after policy, in order
    excluded_by_policy = models.JSONField(default=list)
    row_count = models.BigIntegerField(null=True, blank=True)
    source_row_count = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    storage_key = models.CharField(max_length=512, blank=True, default="")
    sample_info = models.JSONField(default=dict)
    split_spec = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=SNAPSHOT_STATUS, default="building")
    job_id = models.UUIDField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)


class Experiment(TenantModel):
    project = models.ForeignKey(MLProject, on_delete=models.CASCADE, related_name="experiments")
    snapshot = models.ForeignKey(DatasetSnapshot, on_delete=models.PROTECT, related_name="experiments", null=True,
                                 blank=True)
    name = models.CharField(max_length=200, blank=True, default="")
    algorithm = models.CharField(max_length=40)
    task_type = models.CharField(max_length=16, choices=TASK_TYPES)
    hyperparameters = models.JSONField(default=dict)  # as requested
    effective_params = models.JSONField(default=dict)  # defaults + requested (+ search winner)
    random_state = models.IntegerField(default=42)
    tune = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=EXPERIMENT_STATUS, default="queued")
    metrics = models.JSONField(default=dict)  # {test, train, cv{mean,std,...}, primary_metric, primary_value}
    primary_metric = models.CharField(max_length=40, blank=True, default="")
    primary_value = models.FloatField(null=True, blank=True)
    confusion_matrix = models.JSONField(null=True, blank=True)
    residuals = models.JSONField(null=True, blank=True)
    curves = models.JSONField(default=dict)
    feature_importances = models.JSONField(default=dict)
    shap_summary = models.JSONField(null=True, blank=True)
    reference_stats = models.JSONField(default=dict)
    feature_schema = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    sample_info = models.JSONField(default=dict)
    train_rows = models.IntegerField(null=True, blank=True)
    test_rows = models.IntegerField(null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    recommended = models.BooleanField(default=False)
    recommendation_rank = models.IntegerField(null=True, blank=True)
    override_reason = models.TextField(blank=True, default="")
    artifact_key = models.CharField(max_length=512, blank=True, default="")
    artifact_sha256 = models.CharField(max_length=64, blank=True, default="")
    artifact_size = models.BigIntegerField(null=True, blank=True)
    lib_versions = models.JSONField(default=dict)
    job_id = models.UUIDField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    error_code = models.CharField(max_length=60, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "project", "-created_at"], name="mlexp_org_project_idx")]


class MLModel(TenantModel):
    project = models.ForeignKey(MLProject, on_delete=models.PROTECT, related_name="models")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    task_type = models.CharField(max_length=16, choices=TASK_TYPES)
    target = models.CharField(max_length=255, blank=True, default="")
    label_dataset_id = models.UUIDField(null=True, blank=True)  # labels arriving later (performance monitoring)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="mlmodel_org_name_uniq")]


class ModelVersion(TenantModel):
    model = models.ForeignKey(MLModel, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    experiment = models.ForeignKey(Experiment, on_delete=models.PROTECT, related_name="versions")
    algorithm = models.CharField(max_length=40)
    artifact_key = models.CharField(max_length=512)
    artifact_sha256 = models.CharField(max_length=64)
    artifact_size = models.BigIntegerField(null=True, blank=True)
    feature_schema = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    reference_stats = models.JSONField(default=dict)
    lib_versions = models.JSONField(default=dict)
    dataset_id = models.UUIDField(null=True, blank=True)
    dataset_version_id = models.UUIDField(null=True, blank=True)
    snapshot_id = models.UUIDField(null=True, blank=True)
    stage = models.CharField(max_length=12, choices=STAGES, default="none")
    promoted_by = models.UUIDField(null=True, blank=True)
    promoted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-version",)
        constraints = [
            models.UniqueConstraint(fields=["org", "model", "version"], name="mlversion_org_model_ver_uniq"),
            models.UniqueConstraint(fields=["org", "model"], condition=models.Q(stage="production"),
                                    name="mlversion_one_production_uniq"),
        ]


class PredictionLog(TenantModel):
    """Append-only. Inputs are kept as a sha256 and binned counts (for drift), never as raw rows."""

    model = models.ForeignKey(MLModel, on_delete=models.PROTECT, related_name="prediction_logs")
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="prediction_logs")
    n_rows = models.IntegerField()
    source = models.CharField(max_length=8, choices=PREDICTION_SOURCES)
    latency_ms = models.FloatField()
    caller_type = models.CharField(max_length=8, choices=CALLER_TYPES)
    caller_id = models.UUIDField(null=True, blank=True)
    api_key_prefix = models.CharField(max_length=32, blank=True, default="")
    input_sha256 = models.CharField(max_length=64)
    feature_counts = models.JSONField(default=dict)  # drift.bin_counts output
    explain = models.BooleanField(default=False)
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "model_version", "created_at"], name="mlpredlog_version_idx")]


class DriftReport(TenantModel):
    """Append-only drift / performance report for one model version over a window."""

    model = models.ForeignKey(MLModel, on_delete=models.PROTECT, related_name="drift_reports")
    model_version = models.ForeignKey(ModelVersion, on_delete=models.PROTECT, related_name="drift_reports")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    n_predictions = models.IntegerField(default=0)
    n_rows = models.IntegerField(default=0)
    feature_psi = models.JSONField(default=dict)
    prediction_psi = models.FloatField(null=True, blank=True)
    max_psi = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=DRIFT_STATUS, default="insufficient_data")
    performance = models.JSONField(null=True, blank=True)
    alerts = models.JSONField(default=list)
    trigger = models.CharField(max_length=12, default="scheduled")  # scheduled | manual

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["org", "model", "-created_at"], name="mldrift_model_idx")]
