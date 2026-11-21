from django.db import models

from apps.core.tenancy.models import TenantModel


class Pipeline(TenantModel):
    STATUS = [(s, s) for s in ("draft", "active", "paused", "archived")]

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS, default="draft")
    owner_id = models.UUIDField(null=True, blank=True)
    current_version = models.ForeignKey("PipelineVersion", null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")
    max_retries = models.PositiveSmallIntegerField(default=2)
    retry_backoff_s = models.PositiveIntegerField(default=60)
    notify_on_failure = models.BooleanField(default=True)
    output_dataset_id = models.UUIDField(null=True, blank=True)   # created by the first successful run
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="pipeline_org_name_uniq")]
        indexes = [models.Index(fields=["org", "status"], name="pipeline_org_status_idx")]

    def __str__(self) -> str:
        return self.name


class PipelineVersion(TenantModel):
    """Immutable DAG snapshot. Runs always reference the version they executed."""

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    dag = models.JSONField()
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "pipeline", "version"], name="pipelinever_org_uniq")]
        ordering = ["-version"]


class PipelineRun(TenantModel):
    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed", "cancelled")]
    TRIGGERS = [(t, t) for t in ("manual", "schedule", "api", "retry", "automation")]
    ACTIVE = ("queued", "running")

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="runs")
    pipeline_version = models.ForeignKey(PipelineVersion, on_delete=models.PROTECT, related_name="runs")
    number = models.PositiveIntegerField()                       # per-pipeline run number (#1, #2, ...)
    trigger = models.CharField(max_length=16, choices=TRIGGERS, default="manual")
    status = models.CharField(max_length=16, choices=STATUS, default="queued")
    attempt = models.PositiveSmallIntegerField(default=1)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    parent_run = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    triggered_by = models.UUIDField(null=True, blank=True)
    job_id = models.UUIDField(null=True, blank=True)
    ingest_job_id = models.UUIDField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.BigIntegerField(null=True, blank=True)
    rows_in = models.BigIntegerField(null=True, blank=True)
    rows_out = models.BigIntegerField(null=True, blank=True)
    rows_rejected = models.BigIntegerField(null=True, blank=True)
    output_dataset_id = models.UUIDField(null=True, blank=True)
    output_dataset_version_id = models.UUIDField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.CharField(max_length=500, blank=True, default="")
    error_detail = models.JSONField(null=True, blank=True)
    quality_summary = models.JSONField(null=True, blank=True)
    steps = models.JSONField(default=list, blank=True)
    attempts = models.JSONField(default=list, blank=True)       # [{attempt, started_at, finished_at, error_code}]

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "pipeline", "number"], name="pipelinerun_org_num_uniq")]
        indexes = [
            models.Index(fields=["org", "pipeline", "-created_at"], name="pipelinerun_org_pl_idx"),
            models.Index(fields=["org", "status"], name="pipelinerun_org_status_idx"),
            models.Index(fields=["org", "-finished_at"], name="pipelinerun_org_fin_idx"),
        ]


class PipelineRunLog(TenantModel):
    """Append-only run log lines (UPDATE/DELETE blocked by trigger)."""

    LEVELS = [(lv, lv) for lv in ("debug", "info", "warning", "error")]

    run = models.ForeignKey(PipelineRun, on_delete=models.DO_NOTHING, related_name="logs", db_constraint=True)
    ts = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10, choices=LEVELS, default="info")
    step_key = models.CharField(max_length=40, blank=True, default="")
    attempt = models.PositiveSmallIntegerField(default=1)
    message = models.CharField(max_length=1000)
    context = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "run", "ts"], name="pipelinelog_org_run_idx")]
        ordering = ["ts", "id"]


class PipelineSchedule(TenantModel):
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="schedules")
    cron = models.CharField(max_length=100)
    timezone = models.CharField(max_length=64, default="UTC")
    enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)
    last_run_id = models.UUIDField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "pipeline"], name="pipelinesched_org_pl_uniq")]
        indexes = [models.Index(fields=["enabled", "next_run_at"], name="pipelinesched_due_idx")]
