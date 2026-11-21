"""Platform super-admin tables (GLOBAL, not tenant-scoped; written and read on the `ops` alias).

    PlatformAuditEvent  append-only log of every platform-admin action (org suspend/reactivate, quota changes,
                        platform-admin grants). Never deleted.
    AppLogRecord        bounded ring of recent WARNING+ application log records (logs.DatabaseLogHandler), trimmed
                        to PLATFORM_LOG_MAX_ROWS. Holds log metadata and messages, never tenant data rows.
"""

from django.db import models

from apps.core.tenancy.models import uuid7


class PlatformAuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_id = models.UUIDField(null=True, blank=True)  # NULL = a management command / the system
    actor_email = models.CharField(max_length=254, blank=True, default="")
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=60, blank=True, default="")
    target_id = models.CharField(max_length=80, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["target_type", "target_id", "-occurred_at"], name="platform_audit_target_idx")]


class AppLogRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(db_index=True)
    level = models.PositiveSmallIntegerField(db_index=True)
    level_name = models.CharField(max_length=10)
    logger = models.CharField(max_length=120, db_index=True)
    message = models.TextField()
    where = models.CharField(max_length=200, blank=True, default="")  # module:function:line
    exc_type = models.CharField(max_length=120, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")
    process = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["level", "-id"], name="app_log_level_idx")]
