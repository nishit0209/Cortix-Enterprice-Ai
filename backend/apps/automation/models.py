"""Workflow automation, approvals and agents (API_SPECIFICATION Phase 10, DATABASE_SCHEMA §11, CONTRACT "W5").

Every model is a TenantModel with RLS (migration 0001). A workflow's steps live in an immutable, validated
`WorkflowVersion.graph`; a run snapshots the graph it executes, so publishing a new version never changes a run that
is already in flight.
"""

from __future__ import annotations

from django.db import models

from apps.core.tenancy.models import TenantModel

IMPACTS = [("low", "low"), ("medium", "medium"), ("high", "high")]


class Workflow(TenantModel):
    STATUS = [("draft", "draft"), ("active", "active"), ("paused", "paused"), ("archived", "archived")]

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    owner_id = models.UUIDField()
    status = models.CharField(max_length=16, choices=STATUS, default="draft")
    current_version = models.ForeignKey("WorkflowVersion", null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="automation_workflow_org_name_uniq")]
        indexes = [models.Index(fields=["org", "status"], name="automation_wf_org_status_idx")]

    def __str__(self) -> str:
        return self.name


class WorkflowVersion(TenantModel):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    graph = models.JSONField()
    created_by = models.UUIDField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "workflow", "version"],
                                               name="automation_wfversion_org_wf_version_uniq")]


class WorkflowTrigger(TenantModel):
    TYPES = [("manual", "manual"), ("schedule", "schedule"), ("webhook", "webhook"), ("event", "event")]

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="triggers")
    type = models.CharField(max_length=16, choices=TYPES)
    config = models.JSONField(default=dict, blank=True)
    # webhook only: the HMAC secret, envelope-encrypted (apps.core.crypto); shown to the creator exactly once
    secret_encrypted = models.JSONField(null=True, blank=True)
    secret_hint = models.CharField(max_length=8, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.UUIDField()
    next_run_at = models.DateTimeField(null=True, blank=True)  # schedule only
    last_fired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "type", "is_active"], name="automation_trig_type_idx")]


class WebhookDelivery(TenantModel):
    """One accepted webhook call. The unique signature digest makes a replayed (identical) request fail."""

    trigger = models.ForeignKey(WorkflowTrigger, on_delete=models.CASCADE, related_name="deliveries")
    signature_sha256 = models.CharField(max_length=64)
    run_id = models.UUIDField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "trigger", "signature_sha256"],
                                               name="automation_webhook_replay_uniq")]


class AgentConfig(TenantModel):
    TIERS = [("fast", "fast"), ("reasoning", "reasoning")]

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    owner_id = models.UUIDField()
    model_tier = models.CharField(max_length=16, choices=TIERS, default="fast")
    allowed_tools = models.JSONField(default=list)
    tool_scopes = models.JSONField(default=dict, blank=True)
    max_steps = models.PositiveSmallIntegerField(default=8)
    approval_required_for = models.JSONField(default=list, blank=True)  # impacts and/or tool kinds
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "name"], name="automation_agent_org_name_uniq")]


class WorkflowRun(TenantModel):
    STATUS = [("queued", "queued"), ("running", "running"), ("waiting", "waiting"),
              ("waiting_approval", "waiting_approval"), ("succeeded", "succeeded"), ("failed", "failed"),
              ("cancelled", "cancelled")]
    TERMINAL = ("succeeded", "failed", "cancelled")

    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.CASCADE, related_name="runs")
    version = models.ForeignKey(WorkflowVersion, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    agent = models.ForeignKey(AgentConfig, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    trigger = models.ForeignKey(WorkflowTrigger, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    trigger_type = models.CharField(max_length=16, default="manual")
    actor_id = models.UUIDField()  # the user the run acts for; permissions are re-checked before every step
    graph = models.JSONField()     # snapshot of the executed graph
    status = models.CharField(max_length=20, choices=STATUS, default="queued")
    context = models.JSONField(default=dict, blank=True)  # {"input", "trigger", "steps": {key: {...}}}
    error = models.JSONField(null=True, blank=True)
    job_id = models.UUIDField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["org", "workflow", "-created_at"], name="automation_run_wf_idx"),
            models.Index(fields=["org", "status"], name="automation_run_status_idx"),
        ]


class WorkflowStepRun(TenantModel):
    STATUS = [("pending", "pending"), ("running", "running"), ("waiting", "waiting"),
              ("waiting_approval", "waiting_approval"), ("retrying", "retrying"), ("succeeded", "succeeded"),
              ("failed", "failed"), ("skipped", "skipped"), ("cancelled", "cancelled")]
    TERMINAL = ("succeeded", "failed", "skipped", "cancelled")

    run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="steps")
    step_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=64)
    position = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS, default="pending")
    attempt = models.PositiveSmallIntegerField(default=0)
    input = models.JSONField(null=True, blank=True)
    output = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)
    history = models.JSONField(default=list, blank=True)  # earlier failed attempts
    approval_id = models.UUIDField(null=True, blank=True)
    resume_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["org", "run", "step_key"],
                                               name="automation_steprun_org_run_key_uniq")]
        indexes = [models.Index(fields=["org", "status", "resume_at"], name="automation_step_resume_idx")]


class ApprovalRequest(TenantModel):
    STATUS = [("pending", "pending"), ("approved", "approved"), ("rejected", "rejected"), ("expired", "expired"),
              ("cancelled", "cancelled")]

    kind = models.CharField(max_length=40)  # workflow_step | agent_action | model_promotion | ...
    title = models.CharField(max_length=200)
    payload = models.JSONField(default=dict, blank=True)
    source_id = models.UUIDField(null=True, blank=True)
    approver_permission = models.CharField(max_length=60)
    impact = models.CharField(max_length=8, choices=IMPACTS, default="low")
    status = models.CharField(max_length=16, choices=STATUS, default="pending")
    requested_by = models.UUIDField(null=True, blank=True)
    decided_by = models.UUIDField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["org", "status", "-created_at"], name="automation_approval_status_idx")]
