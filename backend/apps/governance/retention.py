"""Data retention (API_SPECIFICATION Phase 5 `retention-policies`, `governance.retention.edit`).

A RetentionPolicy names ONE supported target, `retain_days` and an action (`delete` | `archive`). Targets are
registered by the app that owns the data (lower layers cannot import higher ones, so every app registers its own
targets in `AppConfig.ready()`):

    from apps.governance.retention import RetentionTarget, register_target
    register_target(RetentionTarget(key="bi.conversations", label="...", actions=("delete", "archive"),
                                    count=count_fn, apply=apply_fn))

`count(cutoff, action) -> int` and `apply(cutoff, action) -> int` run INSIDE the org's tenant context (RLS applies,
so they only ever see the active org) and must be idempotent.

Supported targets (W4):
    notifications      core.Notification older than the cutoff                         delete
    jobs               finished core.Job rows (succeeded/failed/cancelled)               delete
    bi.conversations   BI conversations whose last message is older than the cutoff      delete | archive
                       (archive = keep the questions/answers, drop the stored result rows)

NEVER retention targets (append-only evidence, deleted by nobody): audit events (`core.AuditEvent`), access
history, BI query audit, AI request logs, the AI consent ledger and the platform audit log. Short-lived append-only
job events are purged by the platform job `core.purge_job_events`, not by tenant policies.

Legal hold: while the org setting `legal_hold` is true, nothing is deleted or archived (dry runs still count).

The periodic job `governance.retention` (every hour) dry-runs every active policy and applies the ones in
`enforce` mode; every apply is audited (`governance.retention.apply`, actor `system` for the job).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import connections, transaction
from django.utils import timezone

from apps.core import audit
from apps.core.tenancy import tenant_context

log = logging.getLogger("nexora.governance.retention")

PERIODIC_NAME = "governance.retention"
PERIODIC_EVERY_S = 3600
MAX_RETAIN_DAYS = 3650
NEVER_TARGETS = frozenset({"audit_events", "access_history", "bi.query_audit", "ai.request_logs",
                           "ai.consents", "platform.audit_events", "job_events"})
JOB_TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass(frozen=True)
class RetentionTarget:
    key: str
    label: str
    description: str
    actions: tuple[str, ...]
    count: Callable[[datetime, str], int]
    apply: Callable[[datetime, str], int]


_targets: dict[str, RetentionTarget] = {}


def register_target(target: RetentionTarget) -> None:
    if target.key in NEVER_TARGETS:
        raise ValueError(f"{target.key} is append-only evidence and can never be a retention target")
    if not target.actions or set(target.actions) - {"delete", "archive"}:
        raise ValueError("actions must be a non-empty subset of ('delete', 'archive')")
    _targets[target.key] = target


def unregister_target(key: str) -> None:
    _targets.pop(key, None)


def targets() -> list[RetentionTarget]:
    return [_targets[k] for k in sorted(_targets)]


def get_target(key: str) -> RetentionTarget | None:
    return _targets.get(key)


def target_payload(t: RetentionTarget) -> dict:
    return {"key": t.key, "label": t.label, "description": t.description, "actions": list(t.actions)}


def cutoff_for(retain_days: int, now: datetime | None = None) -> datetime:
    return (now or timezone.now()) - timedelta(days=int(retain_days))


def legal_hold_active() -> bool:
    from apps.orgs.services import get_current_org

    try:
        return bool((get_current_org().settings or {}).get("legal_hold"))
    except Exception:  # noqa: BLE001 - fail SAFE: an unreadable org setting blocks deletion
        log.warning("retention: could not read the legal-hold setting; treating it as active", exc_info=True)
        return True


# ------------------------------------------------------------------------------------------------ run one policy
class RetentionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def dry_run(policy, *, now: datetime | None = None) -> dict:
    """Count what `policy` would affect right now (no change) and record it on the policy. Tenant context."""
    target = get_target(policy.target)
    if target is None:
        raise RetentionError("retention_target_unsupported", f"Unsupported retention target {policy.target!r}.")
    now = now or timezone.now()
    cutoff = cutoff_for(policy.retain_days, now)
    matched = int(target.count(cutoff, policy.action))
    result = {"cutoff": cutoff.isoformat(), "matched": matched, "action": policy.action, "at": now.isoformat(),
              "legal_hold": legal_hold_active()}
    policy.last_dry_run_at = now
    policy.last_dry_run = result
    policy.save(update_fields=["last_dry_run_at", "last_dry_run", "updated_at"])
    return result


def apply(policy, *, trigger: str = "manual", request=None, now: datetime | None = None) -> dict:
    """Delete/archive what `policy` covers (tenant context). Requires a prior dry run; refused under legal hold.
    Audited as `governance.retention.apply` (also when nothing matched)."""
    target = get_target(policy.target)
    if target is None:
        raise RetentionError("retention_target_unsupported", f"Unsupported retention target {policy.target!r}.")
    if policy.action not in target.actions:
        raise RetentionError("retention_action_unsupported",
                             f"{policy.target} supports {', '.join(target.actions)}, not {policy.action}.")
    if policy.last_dry_run_at is None:
        raise RetentionError("retention_dry_run_required", "Run a dry run of this policy first.")
    now = now or timezone.now()
    cutoff = cutoff_for(policy.retain_days, now)
    if legal_hold_active():
        result = {"cutoff": cutoff.isoformat(), "affected": 0, "action": policy.action, "at": now.isoformat(),
                  "trigger": trigger, "skipped": "legal_hold"}
    else:
        with transaction.atomic():
            affected = int(target.apply(cutoff, policy.action))
        result = {"cutoff": cutoff.isoformat(), "affected": affected, "action": policy.action,
                  "at": now.isoformat(), "trigger": trigger}
    policy.last_applied_at = now
    policy.last_result = result
    policy.save(update_fields=["last_applied_at", "last_result", "updated_at"])
    audit.record("governance.retention.apply", target=policy, request=request,
                 actor=("system", None) if trigger == "schedule" else None,
                 metadata={"target": policy.target, "retain_days": policy.retain_days, **result})
    return result


# ------------------------------------------------------------------------------------------------ periodic job
def orgs_with_active_policies() -> list[UUID]:
    """Org ids that have an active policy. Platform read on the `ops` alias (ids only, no tenant rows)."""
    with connections["ops"].cursor() as cur:
        cur.execute("SELECT DISTINCT org_id FROM governance_retentionpolicy WHERE is_active ORDER BY org_id")
        return [row[0] for row in cur.fetchall()]


def run_for_current_org(*, now: datetime | None = None) -> dict:
    """Dry-run every active policy of the active org and apply the `enforce` ones. One failure never stops the
    others. Returns {policy_id: {"dry_run": ..., "applied": ... | None, "error": code | None}}."""
    from .models import RetentionPolicy

    out: dict[str, dict] = {}
    for policy in RetentionPolicy.objects.filter(is_active=True).order_by("created_at"):
        entry: dict = {"target": policy.target, "dry_run": None, "applied": None, "error": None}
        try:
            with transaction.atomic():
                entry["dry_run"] = dry_run(policy, now=now)
                if policy.mode == "enforce":
                    entry["applied"] = apply(policy, trigger="schedule", now=now)
        except RetentionError as exc:
            entry["error"] = exc.code
        except Exception:  # noqa: BLE001 - one broken target never stops the others
            log.exception("retention: policy %s (%s) failed", policy.id, policy.target)
            entry["error"] = "retention_failed"
        out[str(policy.id)] = entry
    return out


def run_retention() -> dict:
    """The `governance.retention` periodic job: every org with an active policy, each in its own tenant context."""
    summary: dict[str, dict] = {}
    for org_id in orgs_with_active_policies():
        try:
            with tenant_context(org_id):
                summary[str(org_id)] = run_for_current_org()
        except Exception:  # noqa: BLE001
            log.exception("retention: org %s failed", org_id)
    return summary


# ------------------------------------------------------------------------------------------------ core targets
def _notifications(cutoff: datetime, action: str):
    from apps.core.models import Notification

    return Notification.objects.filter(created_at__lt=cutoff)


def _jobs(cutoff: datetime, action: str):
    from apps.core.models import Job

    return Job.objects.filter(status__in=JOB_TERMINAL, finished_at__isnull=False, finished_at__lt=cutoff)


def _delete(qs) -> int:
    deleted, by_model = qs.delete()
    return int(by_model.get(qs.model._meta.label, deleted))


def register_core_targets() -> None:
    register_target(RetentionTarget(
        key="notifications", label="In-app notifications",
        description="Notifications (read or unread) created before the cutoff are deleted.",
        actions=("delete",),
        count=lambda cutoff, action: _notifications(cutoff, action).count(),
        apply=lambda cutoff, action: _delete(_notifications(cutoff, action)),
    ))
    register_target(RetentionTarget(
        key="jobs", label="Finished background jobs",
        description="Succeeded, failed or cancelled jobs that finished before the cutoff are deleted. Queued and "
                    "running jobs are never touched.",
        actions=("delete",),
        count=lambda cutoff, action: _jobs(cutoff, action).count(),
        apply=lambda cutoff, action: _delete(_jobs(cutoff, action)),
    ))


def register_periodic_job() -> None:
    try:
        from apps.core.scheduler import register_periodic
    except ImportError:  # pragma: no cover - the scheduler ships with core
        return
    register_periodic(PERIODIC_NAME, PERIODIC_EVERY_S, run_retention)
