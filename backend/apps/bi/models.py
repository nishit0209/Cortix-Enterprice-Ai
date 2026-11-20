"""Conversational BI (DATABASE_SCHEMA.md §6): conversations, messages, the append-only query audit, saved queries.

Datasets are referenced by UUID (`dataset_id`), never by FK: the datasets app lives in another agent's layer.
"""

from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.tenancy.models import TenantModel

DEFAULT_TITLE = "New conversation"

MESSAGE_ROLES = [("user", "user"), ("assistant", "assistant")]
MESSAGE_STATUSES = [("pending", "pending"), ("ok", "ok"), ("error", "error"), ("clarification", "clarification"),
                    ("blocked", "blocked")]
FEEDBACK = [("up", "up"), ("down", "down")]
SQL_SOURCES = [("cache_exact", "cache_exact"), ("cache_semantic", "cache_semantic"), ("builder", "builder"),
               ("llm", "llm"), ("manual", "manual"), ("dashboard", "dashboard"), ("none", "none")]
VERDICTS = [("ok", "ok"), ("rejected", "rejected"), ("error", "error"), ("blocked", "blocked")]
PURPOSES = [("chat", "chat"), ("manual", "manual"), ("export", "export"), ("saved_query", "saved_query"),
            ("dashboard", "dashboard"), ("policy_preview", "policy_preview")]
WIDGET_KINDS = [("chart", "chart"), ("table", "table"), ("kpi", "kpi"), ("text", "text")]
GRID_COLUMNS = 12


def default_layout() -> dict:
    return {"x": 0, "y": 0, "w": 6, "h": 4}


class Conversation(TenantModel):
    """A chat thread of one user about one dataset. Only its owner can see it (others get 404)."""

    user_id = models.UUIDField()
    dataset_id = models.UUIDField()
    title = models.CharField(max_length=200, default=DEFAULT_TITLE)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "user_id", "-created_at"], name="bi_conv_user_idx"),
            models.Index(fields=["org", "dataset_id"], name="bi_conv_dataset_idx"),
        ]


class Message(TenantModel):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=MESSAGE_ROLES)
    content = models.TextField(blank=True, default="")
    status = models.CharField(max_length=14, choices=MESSAGE_STATUSES, default="ok")
    error_code = models.CharField(max_length=64, blank=True, default="")
    # assistant messages: the question they answer (history and thumbs-up promotion need it)
    question = models.TextField(blank=True, default="")
    sql_raw = models.TextField(blank=True, default="")
    sql_executed = models.TextField(blank=True, default="")
    sql_source = models.CharField(max_length=16, blank=True, default="")
    result = models.JSONField(null=True, blank=True)  # {columns, rows (capped), row_count, truncated}
    chart = models.JSONField(null=True, blank=True)  # {type, x, y, series}
    insights = models.JSONField(default=list, blank=True)  # [{kind, text, data?}]
    assumptions = models.JSONField(default=list, blank=True)
    explanation = models.TextField(blank=True, default="")
    chart_hint = models.JSONField(null=True, blank=True)  # the model's hint (kept for cache promotion)
    query_audit_id = models.UUIDField(null=True, blank=True)
    dataset_version_id = models.UUIDField(null=True, blank=True)
    feedback = models.CharField(max_length=4, choices=FEEDBACK, blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(role__in=["user", "assistant"]), name="bi_message_role"),
            models.CheckConstraint(
                condition=models.Q(status__in=[k for k, _ in MESSAGE_STATUSES]), name="bi_message_status"
            ),
        ]
        indexes = [models.Index(fields=["org", "conversation", "created_at"], name="bi_msg_conv_idx")]


class BiQueryAudit(TenantModel):
    """APPEND-ONLY evidence of every guarded query: what was asked, what ran, under which policy, and the
    verdict. Never contains result values."""

    occurred_at = models.DateTimeField(db_index=True)
    user_id = models.UUIDField(null=True, blank=True)
    api_key_id = models.UUIDField(null=True, blank=True)
    dataset_id = models.UUIDField()
    dataset_name = models.CharField(max_length=200, blank=True, default="")
    conversation_id = models.UUIDField(null=True, blank=True)
    message_id = models.UUIDField(null=True, blank=True)
    purpose = models.CharField(max_length=16, choices=PURPOSES, default="chat")
    question = models.TextField(blank=True, default="")
    source = models.CharField(max_length=16, choices=SQL_SOURCES)
    raw_sql = models.TextField(blank=True, default="")
    executed_sql = models.TextField(blank=True, default="")
    params = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=10, choices=VERDICTS)
    reject_code = models.CharField(max_length=64, blank=True, default="")
    message = models.TextField(blank=True, default="")  # guard / error message (no values)
    policy_fingerprint = models.CharField(max_length=128, blank=True, default="")
    executing_role = models.CharField(max_length=128, blank=True, default="")
    target = models.CharField(max_length=64, default="warehouse")
    plan_cost = models.FloatField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    bytes = models.BigIntegerField(null=True, blank=True)
    truncated = models.BooleanField(null=True, blank=True)
    ai_request_ids = ArrayField(models.UUIDField(), default=list, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(verdict__in=[k for k, _ in VERDICTS]), name="bi_audit_verdict"),
            models.CheckConstraint(condition=models.Q(source__in=[k for k, _ in SQL_SOURCES]), name="bi_audit_source"),
        ]
        indexes = [
            models.Index(fields=["org", "-occurred_at"], name="bi_audit_org_time_idx"),
            models.Index(fields=["org", "user_id", "-occurred_at"], name="bi_audit_user_time_idx"),
            models.Index(fields=["org", "dataset_id", "-occurred_at"], name="bi_audit_ds_time_idx"),
        ]


class SavedQuery(TenantModel):
    owner_id = models.UUIDField()
    dataset_id = models.UUIDField()
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    question = models.TextField(blank=True, default="")
    sql = models.TextField()
    chart = models.JSONField(null=True, blank=True)
    is_shared = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "owner_id", "name"], name="bi_saved_query_owner_name_uniq"),
        ]
        indexes = [models.Index(fields=["org", "dataset_id"], name="bi_saved_query_ds_idx")]


class Dashboard(TenantModel):
    """A grid of widgets. Private to its owner unless `is_shared` (then every member with `dashboards.view` sees
    it; only the owner changes it). `is_default` marks the owner's home dashboard (at most one per owner)."""

    owner_id = models.UUIDField()
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_default = models.BooleanField(default=False)
    is_shared = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["org", "owner_id"], condition=models.Q(is_default=True),
                                    name="bi_dashboard_one_default_per_owner"),
        ]
        indexes = [models.Index(fields=["org", "owner_id", "-updated_at"], name="bi_dashboard_owner_idx")]


class DashboardWidget(TenantModel):
    """One tile. Data widgets (chart | table | kpi) reference a SavedQuery and are ALWAYS re-executed through the
    SQL guard under the viewer's policy (rows are never stored here); text widgets carry `text` only."""

    dashboard = models.ForeignKey(Dashboard, on_delete=models.CASCADE, related_name="widgets")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=8, choices=WIDGET_KINDS)
    saved_query = models.ForeignKey(SavedQuery, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    chart = models.JSONField(null=True, blank=True)  # {type, x, y, series}
    layout = models.JSONField(default=default_layout)  # {x, y, w, h} on a GRID_COLUMNS-wide grid
    text = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=[k for k, _ in WIDGET_KINDS]), name="bi_widget_kind"),
        ]
        indexes = [models.Index(fields=["org", "dashboard", "created_at"], name="bi_widget_dashboard_idx")]
