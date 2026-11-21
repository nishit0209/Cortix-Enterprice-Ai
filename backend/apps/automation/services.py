"""Public service API of apps.automation. Other apps import only from here (lazily, see CONTRACT "W5 additions").

    from apps.automation.services import request_approval
    approval = request_approval(kind="model_promotion", title="Promote churn v3 to production",
                                payload={"model_id": ..., "version": 3}, approver_permission="ml.promote",
                                impact="high", requested_by=user_id, source_id=version_id)

    from apps.automation.events import emit          # producers of workflow events
    emit("dataset.ingest_finished", {"dataset_id": str(ds.id)})

Approval decisions run the handler registered for the approval's `kind` (`register_approval_handler`). Built in:
`workflow_step` / `agent_action` (resume the paused run) and `model_promotion` (calls
`apps.ml.services.apply_promotion(approval)` when granted).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit
from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.core.exceptions import Conflict, NexoraError, ReauthFailed, Unprocessable
from apps.core.tenancy import TenantInfo, tenant_context

from . import graph as graphmod
from . import runner
from .models import ApprovalRequest, Workflow, WorkflowRun, WorkflowStepRun, WorkflowTrigger, WorkflowVersion

log = logging.getLogger("nexora.automation")

EVENT_KINDS = ("dataset.ingest_finished", "pipeline.run_failed", "pipeline.run_succeeded", "document.processed")
WEBHOOK_TOLERANCE_S = 300
WEBHOOK_MAX_BYTES = 256 * 1024
TIMESTAMP_HEADER = "X-Nexora-Timestamp"
SIGNATURE_HEADER = "X-Nexora-Signature"
MIN_SCHEDULE_INTERVAL_S = 60
DEFAULT_APPROVAL_TTL = timedelta(days=7)
APPROVAL_KINDS_BUILTIN = ("workflow_step", "agent_action", "model_promotion")


class InvalidWorkflow(Unprocessable):
    default_code = "invalid_workflow"
    default_detail = "The workflow graph is invalid."


class WorkflowNotActive(Conflict):
    default_code = "workflow_not_active"
    default_detail = "The workflow has no published version or is not active."


class WebhookRejected(NexoraError):
    status_code = 401
    default_code = "webhook_signature_invalid"
    default_detail = "The webhook signature is invalid."


# ================================================================================================ workflows
def workflow_payload(wf: Workflow, *, with_graph: bool = False) -> dict:
    cur = wf.current_version
    out = {
        "id": str(wf.id), "name": wf.name, "description": wf.description, "status": wf.status,
        "owner_id": str(wf.owner_id),
        "current_version": cur.version if cur else None,
        "published_at": cur.published_at.isoformat() if cur and cur.published_at else None,
        "created_at": wf.created_at.isoformat(), "updated_at": wf.updated_at.isoformat(),
    }
    if with_graph:
        latest = wf.versions.order_by("-version").first()
        out["graph"] = (cur or latest).graph if (cur or latest) else None
        out["latest_version"] = latest.version if latest else None
        out["triggers"] = [trigger_payload(t) for t in wf.triggers.order_by("created_at")]
    return out


def version_payload(v: WorkflowVersion) -> dict:
    return {"id": str(v.id), "version": v.version, "graph": v.graph,
            "created_by": str(v.created_by) if v.created_by else None, "created_at": v.created_at.isoformat(),
            "published_at": v.published_at.isoformat() if v.published_at else None}


def check_graph(graph) -> None:
    errors = graphmod.validate(graph)
    if errors:
        raise InvalidWorkflow(extra={"errors": [
            {"field": f"graph.{e['step']}.{e['field']}" if e["step"] else f"graph.{e['field']}",
             "code": "invalid", "message": e["message"], "step": e["step"]} for e in errors]})


def create_workflow(*, name: str, owner_id: UUID, description: str = "", graph: dict | None = None,
                    request=None) -> Workflow:
    if Workflow.objects.filter(name=name).exists():
        raise Conflict("A workflow with this name already exists.", code="duplicate_name")
    if graph is not None:
        check_graph(graph)
    wf = Workflow.objects.create(name=name, description=description, owner_id=owner_id)
    if graph is not None:
        WorkflowVersion.objects.create(workflow=wf, version=1, graph=graph, created_by=owner_id)
    audit.record("automation.workflow.create", target=wf, request=request, metadata={"name": name})
    return wf


def update_workflow(wf: Workflow, *, data: dict, request=None) -> Workflow:
    changes = {}
    if "name" in data and data["name"] != wf.name:
        if Workflow.objects.filter(name=data["name"]).exclude(id=wf.id).exists():
            raise Conflict("A workflow with this name already exists.", code="duplicate_name")
        changes["name"] = [wf.name, data["name"]]
        wf.name = data["name"]
    if "description" in data:
        wf.description = data["description"]
    if "status" in data and data["status"] != wf.status:
        if data["status"] == "active" and wf.current_version_id is None:
            raise WorkflowNotActive("Publish a version before activating the workflow.")
        changes["status"] = [wf.status, data["status"]]
        wf.status = data["status"]
    wf.save()
    if "graph" in data:
        save_version(wf, data["graph"], user_id=request.user.pk if request else wf.owner_id, request=request)
    audit.record("automation.workflow.update", target=wf, request=request, changes=changes)
    return wf


def save_version(wf: Workflow, graph: dict, *, user_id: UUID, request=None) -> WorkflowVersion:
    check_graph(graph)
    last = wf.versions.order_by("-version").first()
    if last is not None and last.published_at is None:  # edit the unpublished draft in place
        last.graph = graph
        last.created_by = user_id
        last.save(update_fields=["graph", "created_by", "updated_at"])
        return last
    v = WorkflowVersion.objects.create(workflow=wf, version=(last.version + 1) if last else 1, graph=graph,
                                       created_by=user_id)
    audit.record("automation.workflow.version", target=wf, request=request, metadata={"version": v.version})
    return v


def publish(wf: Workflow, *, version: int | None = None, request=None) -> WorkflowVersion:
    qs = wf.versions.order_by("-version")
    v = qs.filter(version=version).first() if version else qs.first()
    if v is None:
        raise ValidationError({"version": ["No such version."]} if version else {"graph": ["Save a graph first."]})
    check_graph(v.graph)  # step kinds may have been unregistered since it was saved
    if v.published_at is None:
        v.published_at = timezone.now()
        v.save(update_fields=["published_at", "updated_at"])
    wf.current_version = v
    if wf.status in ("draft", "paused"):
        wf.status = "active"
    wf.save(update_fields=["current_version", "status", "updated_at"])
    audit.record("automation.workflow.publish", target=wf, request=request, metadata={"version": v.version})
    return v


def delete_workflow(wf: Workflow, *, request=None) -> None:
    if wf.runs.exclude(status__in=WorkflowRun.TERMINAL).exists():
        raise Conflict("Cancel the workflow's active runs first.", code="workflow_has_active_runs")
    audit.record("automation.workflow.delete", target=wf, request=request, metadata={"name": wf.name})
    wf.delete()


def start_run(wf: Workflow, *, actor_id: UUID, run_input: dict | None = None, trigger: WorkflowTrigger | None = None,
              trigger_type: str = "manual", trigger_payload: dict | None = None, request=None) -> WorkflowRun:
    if wf.status != "active" or wf.current_version_id is None:
        raise WorkflowNotActive()
    v = wf.current_version
    return runner.create_run(graph=v.graph, actor_id=actor_id, workflow=wf, version=v, trigger=trigger,
                             trigger_type=trigger_type, run_input=run_input, trigger_payload=trigger_payload,
                             request=request)


# ================================================================================================ runs
def run_payload(run: WorkflowRun, *, with_steps: bool = False) -> dict:
    out = {
        "id": str(run.id), "workflow_id": str(run.workflow_id) if run.workflow_id else None,
        "workflow_name": run.workflow.name if run.workflow_id else None,
        "agent_id": str(run.agent_id) if run.agent_id else None,
        "version": run.version.version if run.version_id else None,
        "trigger_type": run.trigger_type, "trigger_id": str(run.trigger_id) if run.trigger_id else None,
        "status": run.status, "actor_id": str(run.actor_id), "error": run.error,
        "job_id": str(run.job_id) if run.job_id else None,
        "input": (run.context or {}).get("input") or {},
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if with_steps:
        out["steps"] = [step_payload(s) for s in run.steps.order_by("position")]
    return out


def step_payload(s: WorkflowStepRun) -> dict:
    return {
        "id": str(s.id), "step_key": s.step_key, "kind": s.kind, "position": s.position, "status": s.status,
        "attempt": s.attempt, "input": s.input, "output": s.output, "error": s.error, "history": s.history or [],
        "approval_id": str(s.approval_id) if s.approval_id else None,
        "resume_at": s.resume_at.isoformat() if s.resume_at else None,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
    }


# ================================================================================================ triggers
def trigger_payload(t: WorkflowTrigger, *, secret: str | None = None) -> dict:
    out = {
        "id": str(t.id), "workflow_id": str(t.workflow_id), "type": t.type, "config": t.config,
        "is_active": t.is_active, "created_by": str(t.created_by),
        "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
        "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
        "created_at": t.created_at.isoformat(),
    }
    if t.type == "webhook":
        out["webhook_url"] = f"/api/v1/hooks/{t.id}"
        out["secret_hint"] = t.secret_hint
        out["signature"] = {"timestamp_header": TIMESTAMP_HEADER, "signature_header": SIGNATURE_HEADER,
                            "algorithm": "HMAC-SHA256 over '<timestamp>.<raw body>'", "format": "sha256=<hex>",
                            "tolerance_s": WEBHOOK_TOLERANCE_S}
    if secret is not None:
        out["secret"] = secret  # shown exactly once
    return out


def clean_trigger_config(ttype: str, config) -> dict:
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValidationError({"config": ["Must be an object."]})
    if ttype == "schedule":
        cron = config.get("cron")
        every = config.get("every_minutes")
        if bool(cron) == bool(every):
            raise ValidationError({"config": ["Give exactly one of 'cron' (5 fields, UTC) or 'every_minutes'."]})
        if cron:
            from croniter import croniter

            if not isinstance(cron, str) or len(cron.split()) != 5 or not croniter.is_valid(cron):
                raise ValidationError({"config.cron": ["Not a valid 5-field cron expression."]})
            base = timezone.now()
            it = croniter(cron, base)
            first = it.get_next(datetime)
            second = it.get_next(datetime)
            if (second - first).total_seconds() < MIN_SCHEDULE_INTERVAL_S:
                raise ValidationError({"config.cron": ["Schedules run at most once a minute."]})
            return {"cron": cron}
        if not isinstance(every, int) or isinstance(every, bool) or not 1 <= every <= 60 * 24 * 31:
            raise ValidationError({"config.every_minutes": ["An integer between 1 and 44640."]})
        return {"every_minutes": every}
    if ttype == "event":
        event = config.get("event")
        if event not in EVENT_KINDS:
            raise ValidationError({"config.event": [f"One of: {', '.join(EVENT_KINDS)}."]})
        flt = config.get("filter") or {}
        if not isinstance(flt, dict) or not all(isinstance(v, str | int | float | bool) for v in flt.values()):
            raise ValidationError({"config.filter": ["An object of payload field -> exact scalar value."]})
        return {"event": event, "filter": flt}
    if config:
        raise ValidationError({"config": [f"A {ttype} trigger takes no configuration."]})
    return {}


def next_schedule_time(config: dict, after: datetime) -> datetime:
    if config.get("cron"):
        from croniter import croniter

        return croniter(config["cron"], after).get_next(datetime)
    return after + timedelta(minutes=int(config["every_minutes"]))


def _new_secret(trigger: WorkflowTrigger) -> str:
    secret = "whsec_" + secrets.token_urlsafe(32)
    trigger.secret_encrypted = encrypt_secret(secret, aad=_secret_aad(trigger))
    trigger.secret_hint = secret[-4:]
    return secret


def _secret_aad(trigger: WorkflowTrigger) -> str:
    return f"org:{trigger.org_id}:webhook:{trigger.id}"


def create_trigger(wf: Workflow, *, ttype: str, config, user_id: UUID, request=None) -> tuple[WorkflowTrigger,
                                                                                               str | None]:
    if ttype not in dict(WorkflowTrigger.TYPES):
        raise ValidationError({"type": ["One of: manual, schedule, webhook, event."]})
    config = clean_trigger_config(ttype, config)
    t = WorkflowTrigger(workflow=wf, type=ttype, config=config, created_by=user_id)
    t.org_id = wf.org_id
    secret = None
    if ttype == "webhook":
        secret = _new_secret(t)
    if ttype == "schedule":
        t.next_run_at = next_schedule_time(config, timezone.now())
    t.save()
    audit.record("automation.trigger.create", target=t, request=request,
                 metadata={"workflow_id": str(wf.id), "type": ttype, "config": config})
    return t, secret


def rotate_secret(t: WorkflowTrigger, *, request=None) -> str:
    if t.type != "webhook":
        raise ValidationError({"type": ["Only webhook triggers have a secret."]})
    secret = _new_secret(t)
    t.save(update_fields=["secret_encrypted", "secret_hint", "updated_at"])
    audit.record("automation.trigger.rotate_secret", target=t, request=request)
    return secret


def set_trigger_active(t: WorkflowTrigger, active: bool, *, request=None) -> WorkflowTrigger:
    t.is_active = bool(active)
    if t.type == "schedule" and t.is_active:
        t.next_run_at = next_schedule_time(t.config, timezone.now())
    t.save(update_fields=["is_active", "next_run_at", "updated_at"])
    audit.record("automation.trigger.update", target=t, request=request, metadata={"is_active": t.is_active})
    return t


def delete_trigger(t: WorkflowTrigger, *, request=None) -> None:
    audit.record("automation.trigger.delete", target=t, request=request, metadata={"type": t.type})
    t.delete()


# ------------------------------------------------------------------------------------------------ webhooks
def _webhook_org(trigger_id: UUID) -> UUID | None:
    """Pre-tenant lookup (SECURITY DEFINER function, migration 0001): the org of an ACTIVE webhook trigger of an
    ACTIVE org, else None. Reveals nothing else."""
    with connection.cursor() as cur:
        cur.execute("SELECT nexora_webhook_trigger_org(%s)", [str(trigger_id)])
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def sign_webhook(secret: str, timestamp: int | str, body: bytes) -> str:
    """The signature a sender puts in `X-Nexora-Signature` (also used by tests and the UI's curl example)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def receive_webhook(trigger_id: UUID, *, body: bytes, headers, now: float | None = None) -> tuple[int, dict]:
    """POST /api/v1/hooks/{trigger_id} (no session). Returns (status, body). Order: unknown -> 404; stale or
    missing timestamp -> 401 `webhook_stale`; bad signature -> 401 `webhook_signature_invalid`; replayed request ->
    409 `webhook_replayed`; inactive workflow -> 409; else 202 {run_id}."""
    if len(body) > WEBHOOK_MAX_BYTES:
        return 413, {"code": "payload_too_large", "detail": f"The body exceeds {WEBHOOK_MAX_BYTES} bytes."}
    org_id = _webhook_org(trigger_id)
    if org_id is None:
        return 404, {"code": "not_found", "detail": "Not found."}
    now = time.time() if now is None else now
    raw_ts = str(headers.get(TIMESTAMP_HEADER) or "").strip()
    signature = str(headers.get(SIGNATURE_HEADER) or "").strip()
    try:
        ts = int(raw_ts)
    except ValueError:
        return 401, {"code": "webhook_stale", "detail": f"Missing or invalid {TIMESTAMP_HEADER} header."}
    if abs(now - ts) > WEBHOOK_TOLERANCE_S:
        return 401, {"code": "webhook_stale", "detail": "The request timestamp is outside the 5 minute window."}
    try:
        with tenant_context(org_id):
            t = WorkflowTrigger.objects.select_related("workflow").filter(id=trigger_id, type="webhook",
                                                                         is_active=True).first()
            if t is None or not t.secret_encrypted:
                return 404, {"code": "not_found", "detail": "Not found."}
            expected = sign_webhook(decrypt_secret(t.secret_encrypted, aad=_secret_aad(t)), raw_ts, body)
            if not hmac.compare_digest(expected.encode(), signature.encode()):
                audit.record("automation.webhook.rejected", target=t, actor=("system", None),
                             metadata={"reason": "signature"})
                return 401, {"code": "webhook_signature_invalid", "detail": "The webhook signature is invalid."}
            try:
                payload = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (UnicodeDecodeError, ValueError):
                return 400, {"code": "invalid_json", "detail": "The body must be JSON."}
            if not isinstance(payload, dict):
                payload = {"data": payload}
            from .models import WebhookDelivery

            digest = hashlib.sha256(signature.encode()).hexdigest()
            try:
                with transaction.atomic():
                    delivery = WebhookDelivery.objects.create(trigger=t, signature_sha256=digest)
            except IntegrityError:
                return 409, {"code": "webhook_replayed", "detail": "This request was already delivered."}
            wf = t.workflow
            if wf.status != "active" or wf.current_version_id is None:
                return 409, {"code": "workflow_not_active", "detail": "The workflow is not active."}
            with _as_trigger_actor(t):
                run = start_run(wf, actor_id=t.created_by, trigger=t, trigger_type="webhook",
                                trigger_payload=payload)
                t.last_fired_at = timezone.now()
                t.save(update_fields=["last_fired_at", "updated_at"])
                delivery.run_id = run.id
                delivery.save(update_fields=["run_id", "updated_at"])
            return 202, {"run_id": str(run.id), "status": run.status}
    except Exception:  # pragma: no cover - never leak internals to an unauthenticated caller
        log.exception("webhook %s failed", trigger_id)
        return 500, {"code": "error", "detail": "The webhook could not be processed."}


class _as_trigger_actor:  # noqa: N801 - context manager
    """Audit rows written while a trigger starts a run are attributed to the trigger's creator."""

    def __init__(self, t: WorkflowTrigger):
        self.actor = ("user", t.created_by)

    def __enter__(self):
        from apps.core.tenancy.context import current_actor

        self._token = current_actor.set(self.actor)

    def __exit__(self, *exc):
        from apps.core.tenancy.context import current_actor

        current_actor.reset(self._token)
        return False


# ================================================================================================ approvals
_approval_handlers: dict[str, Callable[[ApprovalRequest], dict | None]] = {}


def register_approval_handler(kind: str, fn: Callable[[ApprovalRequest], dict | None]) -> None:
    """`fn(approval)` runs (in the decision's transaction) after every decision of an approval of `kind`; it sees
    `approval.status` (approved / rejected / expired / cancelled). Raising aborts the decision."""
    _approval_handlers[kind] = fn


def request_approval(*, kind: str, title: str, payload: dict, approver_permission: str, impact: str = "low",
                     requested_by: UUID | None = None, source_id: UUID | None = None,
                     expires_in: timedelta | None = DEFAULT_APPROVAL_TTL) -> ApprovalRequest:
    """Create a pending approval (inside the org's tenant context) and notify everyone who may decide it."""
    from apps.orgs.permissions_catalog import PERMISSIONS

    if approver_permission not in PERMISSIONS:
        raise ValueError(f"unknown approver permission {approver_permission!r}")
    requested_by = getattr(requested_by, "pk", requested_by)  # a User instance or its id
    if impact not in ("low", "medium", "high"):
        raise ValueError("impact must be low, medium or high")
    approval = ApprovalRequest.objects.create(
        kind=kind, title=str(title)[:200], payload=json.loads(json.dumps(payload or {}, default=str)),
        approver_permission=approver_permission, impact=impact, requested_by=requested_by, source_id=source_id,
        expires_at=timezone.now() + expires_in if expires_in else None)
    audit.record("automation.approval.request", target=approval,
                 metadata={"kind": kind, "impact": impact, "approver_permission": approver_permission})
    try:
        from apps.core.notifications import notify_permission

        notify_permission(approver_permission, "approval.requested", f"Approval needed: {approval.title}",
                          f"Impact: {impact}.", f"/automation/approvals/{approval.id}")
    except Exception:  # noqa: BLE001 - a notification never fails the request
        log.warning("approval notification for %s not sent", approval.id, exc_info=True)
    return approval


def approval_payload(a: ApprovalRequest) -> dict:
    return {
        "id": str(a.id), "kind": a.kind, "title": a.title, "payload": a.payload,
        "source_id": str(a.source_id) if a.source_id else None, "approver_permission": a.approver_permission,
        "impact": a.impact, "status": a.status, "requires_reauth": a.impact == "high",
        "requested_by": str(a.requested_by) if a.requested_by else None,
        "decided_by": str(a.decided_by) if a.decided_by else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None, "comment": a.comment,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None, "result": a.result,
        "created_at": a.created_at.isoformat(),
    }


def visible_approvals(info: TenantInfo, user_id: UUID):
    """Approvals the member may decide (they hold its approver permission) or that they requested."""
    from django.db.models import Q

    return ApprovalRequest.objects.filter(Q(approver_permission__in=sorted(info.permissions)) |
                                          Q(requested_by=user_id))


def decide(approval: ApprovalRequest, *, user, info: TenantInfo, decision: str, comment: str = "",
           password: str | None = None, request=None) -> ApprovalRequest:
    """Approve or reject (inside the request's tenant transaction). The approver must hold the approval's
    permission (else 404: not visible); `impact=high` needs the password (400 `reauth_failed`)."""
    approval = ApprovalRequest.objects.select_for_update().get(id=approval.id)
    if approval.approver_permission not in info.permissions:
        raise NotFound()
    if decision not in ("approve", "reject"):
        raise ValidationError({"decision": ["One of: approve, reject."]})
    if approval.status == "pending" and approval.expires_at and approval.expires_at <= timezone.now():
        _close(approval, "expired", decided_by=None, comment="")
        raise Conflict("The approval request has expired.", code="approval_expired")
    if approval.status != "pending":
        raise Conflict(f"The approval request is already {approval.status}.", code="approval_not_pending")
    if approval.impact == "high" and (not password or not user.check_password(password)):
        audit.record("automation.approval.reauth_failed", target=approval, request=request)
        raise ReauthFailed()
    status = "approved" if decision == "approve" else "rejected"
    try:
        with transaction.atomic():
            _close(approval, status, decided_by=user.pk, comment=comment, request=request)
    except NexoraError:
        raise
    except Exception as exc:
        log.exception("approval %s handler failed", approval.id)
        raise Unprocessable("The approved action could not be applied.", code="approval_apply_failed") from exc
    return approval


def _close(approval: ApprovalRequest, status: str, *, decided_by, comment: str, request=None) -> None:
    approval.status = status
    approval.decided_by = decided_by
    approval.decided_at = timezone.now()
    approval.comment = comment or ""
    approval.save(update_fields=["status", "decided_by", "decided_at", "comment", "updated_at"])
    handler = _approval_handlers.get(approval.kind)
    if handler is not None:
        result = handler(approval)
        if result is not None:
            approval.result = json.loads(json.dumps(result, default=str))
            approval.save(update_fields=["result", "updated_at"])
    audit.record(f"automation.approval.{status}", target=approval, request=request,
                 metadata={"kind": approval.kind, "impact": approval.impact, "comment": comment[:500]})
    if approval.requested_by and status in ("approved", "rejected"):
        try:
            from apps.core.notifications import notify

            notify([approval.requested_by], f"approval.{status}", f"{approval.title}: {status}", comment[:500],
                   f"/automation/approvals/{approval.id}")
        except Exception:  # noqa: BLE001
            log.warning("approval decision notification for %s not sent", approval.id, exc_info=True)


def _resume_from_approval(approval: ApprovalRequest) -> dict | None:
    step = WorkflowStepRun.objects.filter(id=approval.source_id).first() if approval.source_id else None
    if step is None:
        return None
    if approval.status == "approved" and (approval.payload or {}).get("gate") == runner.IMPACT_GATE:
        # the runner's own gate in front of a high-impact step: re-arm the step so it now EXECUTES
        runner.resume_step(step, status="pending")
    elif approval.status == "approved":
        runner.resume_step(step, status="succeeded", output={
            "decision": "approved", "approval_id": str(approval.id),
            "decided_by": str(approval.decided_by) if approval.decided_by else None, "comment": approval.comment})
    elif approval.status in ("rejected", "expired"):
        code = "approval_rejected" if approval.status == "rejected" else "approval_expired"
        runner.resume_step(step, status="failed", error={
            "code": code, "message": f"The approval was {approval.status}.", "approval_id": str(approval.id)})
    return {"run_id": str(step.run_id), "step_key": step.step_key}


def _apply_model_promotion(approval: ApprovalRequest) -> dict | None:
    if approval.status != "approved":
        return None
    try:
        from apps.ml.services import apply_promotion
    except ImportError as exc:
        raise Unprocessable("Model promotion is not available.", code="approval_apply_failed") from exc
    result = apply_promotion(approval)
    return result if isinstance(result, dict) else {"applied": True}


register_approval_handler("workflow_step", _resume_from_approval)
register_approval_handler("agent_action", _resume_from_approval)
register_approval_handler("model_promotion", _apply_model_promotion)


# ================================================================================================ periodic work
def _due_orgs(now: datetime) -> list[UUID]:
    with connection.cursor() as cur:
        cur.execute("SELECT org_id FROM nexora_automation_due_orgs(%s)", [now])
        return [row[0] for row in cur.fetchall()]


def process_due_for_current_org(now: datetime | None = None) -> dict:
    """Fire due schedule triggers, resume elapsed waits/retries and expire approvals (inside a tenant context)."""
    now = now or timezone.now()
    out = {"schedules": 0, "resumed": 0, "expired": 0}
    for t in (WorkflowTrigger.objects.select_related("workflow")
              .filter(type="schedule", is_active=True, next_run_at__lte=now)):
        try:
            with transaction.atomic():
                t.next_run_at = next_schedule_time(t.config, now)
                t.last_fired_at = now
                t.save(update_fields=["next_run_at", "last_fired_at", "updated_at"])
                wf = t.workflow
                if wf.status == "active" and wf.current_version_id:
                    with _as_trigger_actor(t):
                        start_run(wf, actor_id=t.created_by, trigger=t, trigger_type="schedule",
                                  trigger_payload={"scheduled_for": now.isoformat()})
                    out["schedules"] += 1
        except Exception:  # noqa: BLE001 - one broken trigger never stops the others
            log.exception("schedule trigger %s failed", t.id)
    for step in WorkflowStepRun.objects.filter(status__in=("waiting", "retrying"), resume_at__lte=now):
        try:
            with transaction.atomic():
                if step.status == "retrying":
                    runner.resume_step(step, status="pending")
                else:
                    runner.resume_step(step, status="succeeded",
                                       output={**(step.output or {}), "resumed_at": now.isoformat()})
                out["resumed"] += 1
        except Exception:  # noqa: BLE001
            log.exception("resuming step %s failed", step.id)
    for approval in ApprovalRequest.objects.filter(status="pending", expires_at__lte=now):
        try:
            with transaction.atomic():
                _close(approval, "expired", decided_by=None, comment="")
                out["expired"] += 1
        except Exception:  # noqa: BLE001
            log.exception("expiring approval %s failed", approval.id)
    return out


def dispatch_due() -> dict:
    """The `automation.dispatch` periodic job (every 15 s): every org with due work, each in its own context."""
    now = timezone.now()
    summary = {}
    for org_id in _due_orgs(now):
        try:
            with tenant_context(org_id):
                summary[str(org_id)] = process_due_for_current_org(now)
        except Exception:  # noqa: BLE001
            log.exception("automation dispatch for org %s failed", org_id)
    return summary


def step_catalog() -> list[dict]:
    from . import registry

    return [s.public() for s in registry.all_steps()]
