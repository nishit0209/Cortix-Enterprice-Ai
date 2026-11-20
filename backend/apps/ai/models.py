"""AI platform persistence.

* AIRequestLog  - one row per Gemini request attempt-group (append-only, tenant, RLS).
* AIUsageDaily  - per org / Pacific day / model counters (tenant, RLS).
* AIOrgConfig   - per org budgets and logging switches (tenant, RLS).
* AIModelQuota  - GLOBAL project quotas per model (Gemini quotas are per Google Cloud project).
"""

from __future__ import annotations

from django.db import models

from apps.core.tenancy.models import TenantModel, uuid7

from .constants import RequestStatus

STATUS_CHOICES = [(s.value, s.value) for s in RequestStatus]


class AIRequestLog(TenantModel):
    """Metadata of every Gemini request (including blocked / not-configured attempts). Append-only."""

    user_id = models.UUIDField(null=True, blank=True)
    feature = models.CharField(max_length=60)
    model = models.CharField(max_length=80)
    prompt_key = models.CharField(max_length=80)
    prompt_version = models.CharField(max_length=20, default="1")
    payload_classes = models.JSONField(default=list, blank=True)
    payload_assets = models.JSONField(default=list, blank=True)
    ai_data_policy = models.CharField(max_length=20)
    gemini_tier = models.CharField(max_length=10)
    priority = models.CharField(max_length=16, default="interactive")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cached_tokens = models.IntegerField(default=0)
    latency_ms = models.IntegerField(default=0)
    attempts = models.SmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error_code = models.CharField(max_length=60, blank=True, default="")
    prompt_sha256 = models.CharField(max_length=64, blank=True, default="")
    # Redacted prompt text, ONLY when AIOrgConfig.log_prompt_text is enabled (retention: 30 days).
    prompt_text = models.TextField(blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["org", "-created_at"], name="ai_reqlog_org_time_idx"),
            models.Index(fields=["org", "feature", "-created_at"], name="ai_reqlog_org_feat_idx"),
        ]
        ordering = ["-created_at"]


class AIUsageDaily(TenantModel):
    """Counters per org, Pacific quota day and model. Updated with F() increments."""

    day = models.DateField()
    model = models.CharField(max_length=80)
    requests = models.IntegerField(default=0)
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cached_tokens = models.BigIntegerField(default=0)
    blocked = models.IntegerField(default=0)
    errors = models.IntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "day", "model"], name="ai_usage_org_day_model_uniq")]
        indexes = [models.Index(fields=["org", "-day"], name="ai_usage_org_day_idx")]
        ordering = ["-day", "model"]


class AIOrgConfig(TenantModel):
    """Per-org AI budgets. Absent row => defaults below."""

    DEFAULT_DAILY_REQUEST_BUDGET = 50
    DEFAULT_DAILY_TOKEN_BUDGET = 250_000
    DEFAULT_PER_USER_DAILY_REQUESTS = 20

    daily_request_budget = models.PositiveIntegerField(default=DEFAULT_DAILY_REQUEST_BUDGET)
    daily_token_budget = models.PositiveIntegerField(default=DEFAULT_DAILY_TOKEN_BUDGET)
    per_user_daily_requests = models.PositiveIntegerField(default=DEFAULT_PER_USER_DAILY_REQUESTS)
    log_prompt_text = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org"], name="ai_orgconfig_org_uniq")]


class AIModelQuota(models.Model):
    """GLOBAL (not tenant-scoped): the platform Google Cloud project's limits per model.

    Values are read manually from AI Studio (free-tier limits are not published) and entered by a
    platform admin; `verified_at` records when. A 10% safety margin is applied by the limiter.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    model = models.CharField(max_length=80, unique=True)
    rpm = models.PositiveIntegerField()
    tpm = models.PositiveIntegerField()
    rpd = models.PositiveIntegerField()
    verified_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.model} rpm={self.rpm} tpm={self.tpm} rpd={self.rpd}"


class AIQuotaCounter(models.Model):
    """GLOBAL quota counters and breaker state when no Redis is configured (local PostgreSQL-only runtime).

    One row per limiter key (the same keys as the Redis limiter: `ai:q:...`, `ai:breaker:<model>`). Rows are
    locked with SELECT ... FOR UPDATE while a reservation is evaluated, so concurrent workers cannot overshoot a
    limit. Keys contain org ids but no tenant data; expired rows are purged by the `ai.purge_quota_counters` job.
    """

    key = models.CharField(max_length=200, primary_key=True)
    value = models.TextField(null=True, blank=True)  # NULL = absent (placeholder row created for locking)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"], name="ai_quotactr_exp_idx")]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.key}={self.value}"
