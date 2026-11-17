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
