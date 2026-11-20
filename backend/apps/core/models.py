from django.db import models

from .tenancy.models import TenantModel


class AuditEvent(TenantModel):
    """Append-only audit trail (UPDATE/DELETE blocked by trigger + revoked grants)."""

    ACTOR_TYPES = [("user", "user"), ("api_key", "api_key"), ("system", "system"), ("agent", "agent")]

    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_type = models.CharField(max_length=16, choices=ACTOR_TYPES, default="system")
    actor_id = models.UUIDField(null=True, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=60, blank=True, default="")
    target_id = models.CharField(max_length=64, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    changes = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    seq = models.BigIntegerField(null=True, blank=True)
    prev_hash = models.BinaryField(null=True, blank=True)
    hash = models.BinaryField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "-occurred_at"], name="audit_org_time_idx"),
            models.Index(fields=["org", "target_type", "target_id"], name="audit_org_target_idx"),
        ]
        ordering = ["-occurred_at"]


class Job(TenantModel):
    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "failed", "cancelled")]

    user_id = models.UUIDField(null=True, blank=True)
    type = models.CharField(max_length=60)
    status = models.CharField(max_length=16, choices=STATUS, default="queued", db_index=True)
    celery_task_id = models.CharField(max_length=64, blank=True, default="")
    progress = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    message = models.CharField(max_length=300, blank=True, default="")
    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=80, null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "idempotency_key"], name="job_org_idem_uniq"),
        ]
        indexes = [models.Index(fields=["org", "-created_at"], name="job_org_time_idx")]


class JobEvent(TenantModel):
    """APPEND-ONLY job event (JOB_EVENTS_BACKEND="db"): the SSE endpoint polls these rows; `seq` is the per-job,
    strictly increasing SSE event id (`Last-Event-ID`). Purged after JOB_EVENT_RETENTION_DAYS by nexora_ops.

    `job_id` is deliberately not a FK: events are written on the durable alias, possibly before the job row's
    creating transaction has committed."""

    job_id = models.UUIDField()
    seq = models.BigIntegerField()
    event = models.CharField(max_length=32)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "job_id", "seq"], name="core_jobevent_seq_uniq")]
        indexes = [models.Index(fields=["created_at"], name="core_jobevent_created_idx")]


NOTIFICATION_KIND_MAX = 60


class Notification(TenantModel):
    """An in-app notification for one user of one org (CONTRACT "W3 additions / Notifications")."""

    user_id = models.UUIDField()
    kind = models.CharField(max_length=NOTIFICATION_KIND_MAX)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    link = models.CharField(max_length=500, blank=True, default="")
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "user_id", "-created_at"], name="core_notif_user_time_idx"),
            models.Index(fields=["org", "user_id"], condition=models.Q(read_at__isnull=True),
                         name="core_notif_unread_idx"),
        ]
