"""The workflow runner: a long-running TenantTask that advances a run step by step.

Every step executes in ONE short tenant transaction that also holds the run row lock (`SELECT ... FOR UPDATE`), so
two workers can never advance the same run, and a crash leaves the step `pending` (it is simply executed again).
Before every step the actor's CURRENT membership and permissions are re-read (no privilege escalation): the actor
needs `automation.run` plus the step kind's own permission.

Pauses: a handler raises `PauseRun` -> the step waits for an approval decision (`waiting_approval`) or a time
(`waiting`); `resume_step()` finishes it later and re-queues the run. Retries: `retries` / `retry_backoff_s` per
step (immediate, or a timed `retrying` state picked up by the periodic dispatcher). Timeouts: `timeout_s` per step is
enforced as the database `statement_timeout` of the step's transaction AND at the step boundary (a late result is
rolled back and counts as `step_timeout`).
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

from celery import shared_task
from django.db import connection, transaction
from django.utils import timezone

from apps.core import audit, jobs
from apps.core.tenancy import TenantInfo
from apps.core.tenancy.context import current_actor, current_tenant
from apps.core.tenancy.tasks import TenantTask, tenant_step

from . import graph as graphmod
from . import registry
from .models import WorkflowRun, WorkflowStepRun

log = logging.getLogger("nexora.automation")

MAX_OUTPUT_BYTES = 256 * 1024
MAX_ITERATIONS_PER_DRIVE = 500
RUN_PERMISSION = "automation.run"
IMPACT_GATE = "impact"  # payload marker of the approval the runner itself requests before a high-impact step
IMPACT_APPROVER = "approvals.decide"


# ------------------------------------------------------------------------------------------------ creation
def create_run(*, graph: dict, actor_id: UUID, workflow=None, version=None, agent=None, trigger=None,
               trigger_type: str = "manual", run_input: dict | None = None, trigger_payload: dict | None = None,
               request=None) -> WorkflowRun:
    """Create a queued run with one pending step row per step (inside the org's tenant context) and start it after
    the surrounding transaction commits."""
    order = graphmod.topological_order(graph["steps"])
    if order is None:  # validated graphs never get here
        raise ValueError("graph has a cycle")
    steps = graphmod.step_map(graph)
    title = workflow.name if workflow is not None else (agent.name if agent is not None else "run")
    job = jobs.create_job("automation.run", user_id=actor_id, message=f"Workflow '{title}' queued")
    run = WorkflowRun.objects.create(
        workflow=workflow, version=version, agent=agent, trigger=trigger, trigger_type=trigger_type,
        actor_id=actor_id, graph=graph, status="queued", job_id=job.id,
        context={"input": run_input or {}, "trigger": {"type": trigger_type, "payload": trigger_payload or {}},
                 "steps": {}},
    )
    WorkflowStepRun.objects.bulk_create([
        WorkflowStepRun(org_id=run.org_id, run=run, step_key=key, kind=steps[key]["kind"], position=i)
        for i, key in enumerate(order)
    ])
    audit.record("automation.run.create", target=run, request=request,
                 metadata={"workflow_id": str(workflow.id) if workflow else None,
                           "agent_id": str(agent.id) if agent else None, "trigger_type": trigger_type})
    enqueue(run)
    return run


def enqueue(run: WorkflowRun) -> None:
    kwargs = {"org_id": str(run.org_id), "run_id": str(run.id), "actor_id": str(run.actor_id)}
    transaction.on_commit(lambda: execute_run.delay(**kwargs))


@shared_task(base=TenantTask, name="apps.automation.runner.execute_run", long_running=True)
def execute_run(*, org_id: str, run_id: str, actor_id: str | None = None) -> str:
    return drive(UUID(str(org_id)), UUID(str(run_id)))



def drive(org_id: UUID, run_id: UUID) -> str:
    """Advance the run until it finishes or pauses. Returns the run's status."""
    status = "missing"
    for _ in range(MAX_ITERATIONS_PER_DRIVE):
        with tenant_step(org_id):
            run = WorkflowRun.objects.select_for_update().filter(id=run_id).first()
            if run is None:
                return "missing"
            status = run.status
            if status not in ("queued", "running"):
                return status
            progressed = _advance(run)
            status = run.status
        if not progressed:
            return status
    log.warning("automation run %s did not settle after %s iterations", run_id, MAX_ITERATIONS_PER_DRIVE)
    return status


# ------------------------------------------------------------------------------------------------ advancing
def _advance(run: WorkflowRun) -> bool:
    """One unit of progress (inside the locked tenant transaction). True when the caller should continue."""
    now = timezone.now()
    if run.status == "queued":
        run.status = "running"
        run.started_at = run.started_at or now
        run.save(update_fields=["status", "started_at", "updated_at"])
        _job_update(run, "running", "Running")
    steps = list(WorkflowStepRun.objects.filter(run=run).order_by("position"))
    by_key = {s.step_key: s for s in steps}
    for s in steps:
        if s.status == "waiting_approval":
            return _set_run_status(run, "waiting_approval")
        if s.status in ("waiting", "retrying"):
            return _set_run_status(run, "waiting")
    defs = graphmod.step_map(run.graph)
    for s in steps:
        if s.status != "pending":
            continue
        sdef = defs.get(s.step_key, {})
        deps = [by_key[d] for d in sdef.get("depends_on", []) if d in by_key]
        if any(d.status not in WorkflowStepRun.TERMINAL for d in deps):
            continue
        if any(d.status != "succeeded" for d in deps) or not _gate_open(run, sdef, deps):
            _finish_step(run, s, "skipped", output={"reason": "condition"})
            return True
        return _execute(run, s, sdef)
    _finalize(run, steps)
    return False


def _gate_open(run: WorkflowRun, sdef: dict, deps: list[WorkflowStepRun]) -> bool:
    want = bool(sdef.get("when", True))
    for d in deps:
        if d.kind == "condition":
            result = bool(((run.context.get("steps") or {}).get(d.step_key) or {}).get("output", {}).get("result"))
            if result != want:
                return False
    return True


def _set_run_status(run: WorkflowRun, status: str) -> bool:
    if run.status != status:
        run.status = status
        run.save(update_fields=["status", "updated_at"])
        _job_update(run, "running", "Waiting for approval" if status == "waiting_approval" else "Waiting")
    return False


def _finalize(run: WorkflowRun, steps: list[WorkflowStepRun], *, failed_run: bool = False) -> None:
    failed = [s for s in steps if s.status == "failed"]
    run.status = "failed" if (failed or failed_run) else "succeeded"
    run.finished_at = timezone.now()
    if failed and not run.error:
        run.error = {"step": failed[0].step_key, **(failed[0].error or {})}
    run.save(update_fields=["status", "finished_at", "error", "updated_at"])
    audit.record(f"automation.run.{run.status}", target=run, actor=("user", run.actor_id),
                 metadata={"error": run.error} if run.error else {})
    if run.job_id:
        if run.status == "succeeded":
            jobs.finish(run.job_id, result={"run_id": str(run.id), "status": run.status})
        else:
            err = run.error or {}
            jobs.fail(run.job_id, code=str(err.get("code") or "run_failed"),
                      message=str(err.get("message") or "The workflow run failed."))
    _notify_finished(run)


def _notify_finished(run: WorkflowRun) -> None:
    if run.status != "failed":
        return
    try:
        from apps.core.notifications import notify

        name = run.workflow.name if run.workflow_id else (run.agent.name if run.agent_id else "Workflow")
        notify([run.actor_id], "automation.run_failed", f"Workflow '{name}' failed",
               str((run.error or {}).get("message") or "")[:500], f"/automation/runs/{run.id}")
    except Exception:  # noqa: BLE001 - a notification never fails the run
        log.warning("automation: failed-run notification for %s not sent", run.id, exc_info=True)


def _fail_run(run: WorkflowRun, code: str, message: str, *, step: WorkflowStepRun | None = None) -> None:
    run.error = {"step": step.step_key if step else None, "code": code, "message": message}
    for s in WorkflowStepRun.objects.filter(run=run, status__in=("pending", "retrying", "waiting",
                                                                 "waiting_approval")):
        s.status = "cancelled"
        s.finished_at = timezone.now()
        s.save(update_fields=["status", "finished_at", "updated_at"])
    _finalize(run, list(WorkflowStepRun.objects.filter(run=run)), failed_run=True)


# ------------------------------------------------------------------------------------------------ one step
@contextmanager
def _acting_as(info: TenantInfo, actor_id: UUID):
    t1 = current_tenant.set(info)
    t2 = current_actor.set(("user", actor_id))
    try:
        yield
    finally:
        current_actor.reset(t2)
        current_tenant.reset(t1)


def _set_statement_timeout(ms: int) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true)", [str(int(ms))])


def _current_statement_timeout() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')")
        return cur.fetchone()[0]


def _execute(run: WorkflowRun, step: WorkflowStepRun, sdef: dict) -> bool:
    from apps.orgs.services import member_permissions

    info = member_permissions(run.actor_id)
    if info is None:
        _fail_run(run, "actor_not_member", "The user this run acts for is no longer an active member.", step=None)
        return False
    spec = registry.get_step(step.kind)
    config = graphmod.render(sdef.get("config", {}), run.context)
    step.input = config
    timeout_s = int(sdef.get("timeout_s") or graphmod.DEFAULT_TIMEOUT_S)
    error: registry.StepError | None = None
    pause: registry.PauseRun | None = None
    output: dict | None = None
    if spec is None:
        error = registry.StepError("unknown_step_kind", f"Step kind '{step.kind}' is not available.", retryable=False)
    elif RUN_PERMISSION not in info.permissions:
        error = registry.StepPermissionDenied(RUN_PERMISSION)
    elif spec.permission and spec.permission not in info.permissions:
        error = registry.StepPermissionDenied(spec.permission)
    else:
        problems = registry.config_errors(step.kind, config)
        if problems:
            error = registry.StepError("invalid_config", "; ".join(problems)[:1000], retryable=False)
    if error is None and spec.impact == "high" and not _impact_approved(run, step, sdef):
        # a high-impact step NEVER executes before a high-impact approval of exactly this step (or of a directly
        # guarding `approval` step of the same run); the handler is not called until then
        return _pause(run, step, _request_impact_approval(run, step, spec, config))
    step.attempt += 1
    step.started_at = timezone.now()
    if error is None:
        ctx = registry.StepContext(
            org_id=run.org_id, run_id=run.id, step_run_id=step.id, step_key=step.step_key, kind=step.kind,
            actor_id=run.actor_id, permissions=info.permissions, attempt=step.attempt,
            deadline=time.monotonic() + timeout_s, run_context=run.context, step=sdef)
        _job_event(run, "step", {"step_key": step.step_key, "kind": step.kind, "status": "running",
                                 "attempt": step.attempt})
        try:
            with _acting_as(info, run.actor_id), transaction.atomic():
                previous_timeout = _current_statement_timeout()
                _set_statement_timeout(timeout_s * 1000)
                try:
                    output = spec.handler(ctx, config)
                except registry.PauseRun as exc:
                    pause = exc
                ctx.check_deadline()
                if pause is None:
                    output = _clean_output(output)
                _set_statement_timeout_value(previous_timeout)
        except registry.StepError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - a step failure is recorded, never crashes the runner
            log.warning("automation step %s (%s) of run %s failed", step.step_key, step.kind, run.id, exc_info=True)
            error = _as_step_error(exc)
    if pause is not None:
        return _pause(run, step, pause)
    if error is not None:
        return _step_failed(run, step, sdef, error)
    _finish_step(run, step, "succeeded", output=output)
    return True


def _impact_approved(run: WorkflowRun, step: WorkflowStepRun, sdef: dict) -> bool:
    """True when this high-impact step may execute: its own impact-gate approval was granted, or one of its DIRECT
    dependencies is a succeeded `approval` step whose request was a granted high-impact `approvals.decide` one."""
    from .models import ApprovalRequest

    if step.approval_id and ApprovalRequest.objects.filter(
            id=step.approval_id, source_id=step.id, status="approved", impact="high").exists():
        return True
    deps = [d for d in (sdef.get("depends_on") or []) if isinstance(d, str)]
    if not deps:
        return False
    guard_ids = list(WorkflowStepRun.objects.filter(run=run, step_key__in=deps, kind="approval", status="succeeded")
                     .exclude(approval_id=None).values_list("approval_id", flat=True))
    return bool(guard_ids) and ApprovalRequest.objects.filter(
        id__in=guard_ids, status="approved", impact="high", approver_permission=IMPACT_APPROVER).exists()


def _request_impact_approval(run: WorkflowRun, step: WorkflowStepRun, spec: registry.StepSpec,
                             config: dict) -> registry.PauseRun:
    from .services import request_approval

    details = config if len(json.dumps(config, default=str)) <= 8192 else {"truncated": True}
    approval = request_approval(
        kind="agent_action" if run.agent_id else "workflow_step",
        title=f"Run high-impact step '{spec.label or spec.kind}' ({step.step_key})",
        payload={"run_id": str(run.id), "step_key": step.step_key, "step_kind": spec.kind, "gate": IMPACT_GATE,
                 "details": details},
        approver_permission=IMPACT_APPROVER, impact="high", requested_by=run.actor_id, source_id=step.id)
    return registry.PauseRun("approval", approval_id=approval.id,
                             output={"approval_id": str(approval.id), "gate": IMPACT_GATE})


def _set_statement_timeout_value(value: str) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', %s, true)", [value])


def _as_step_error(exc: Exception) -> registry.StepError:
    from django.db import OperationalError

    from apps.core.exceptions import NexoraError

    if isinstance(exc, OperationalError) and "statement timeout" in str(exc).lower():
        return registry.StepError("step_timeout", "The step exceeded its timeout.")
    if isinstance(exc, NexoraError):
        codes = exc.get_codes()
        return registry.StepError(codes if isinstance(codes, str) else "step_failed", str(exc.detail)[:1000],
                                  retryable=exc.status_code >= 500 or exc.status_code == 429)
    from rest_framework.exceptions import APIException

    if isinstance(exc, APIException):
        return registry.StepError("step_failed", str(exc.detail)[:1000], retryable=False)
    return registry.StepError("step_failed", f"{type(exc).__name__}: the step failed unexpectedly.")


def _clean_output(output) -> dict:
    if output is None:
        return {}
    if not isinstance(output, dict):
        raise registry.StepError("invalid_output", "A step must return a JSON object.", retryable=False)
    try:
        encoded = json.dumps(output, default=str)
    except (TypeError, ValueError) as exc:
        raise registry.StepError("invalid_output", "The step output is not JSON-serialisable.",
                                 retryable=False) from exc
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise registry.StepError("output_too_large", f"The step output exceeds {MAX_OUTPUT_BYTES} bytes.",
                                 retryable=False)
    return json.loads(encoded)


def _pause(run: WorkflowRun, step: WorkflowStepRun, pause: registry.PauseRun) -> bool:
    step.status = "waiting_approval" if pause.kind == "approval" else "waiting"
    step.approval_id = pause.approval_id
    step.resume_at = pause.resume_at
    step.output = pause.output or None
    step.save()
    _job_event(run, "step", {"step_key": step.step_key, "kind": step.kind, "status": step.status})
    audit.record("automation.step.paused", target=step, actor=("user", run.actor_id),
                 metadata={"run_id": str(run.id), "kind": step.kind, "pause": pause.kind,
                           "approval_id": str(pause.approval_id) if pause.approval_id else None})
    return _set_run_status(run, "waiting_approval" if pause.kind == "approval" else "waiting")


def _step_failed(run: WorkflowRun, step: WorkflowStepRun, sdef: dict, error: registry.StepError) -> bool:
    err = {"code": error.code, "message": error.message, "attempt": step.attempt}
    retries = int(sdef.get("retries") or 0)
    audit.record("automation.step.failed", target=step, actor=("user", run.actor_id),
                 metadata={"run_id": str(run.id), "kind": step.kind, "code": error.code, "attempt": step.attempt})
    if error.retryable and step.attempt <= retries:
        step.history = [*(step.history or []), err]
        step.error = err
        backoff = int(sdef.get("retry_backoff_s") or 0)
        if backoff > 0:
            step.status = "retrying"
            step.resume_at = timezone.now() + timedelta(seconds=backoff)
            step.save()
            _job_event(run, "step", {"step_key": step.step_key, "status": "retrying", "attempt": step.attempt})
            return _set_run_status(run, "waiting")
        step.status = "pending"
        step.save()
        return True
    step.status = "failed"
    step.error = err
    step.finished_at = timezone.now()
    step.save()
    _record_step_context(run, step)
    _job_event(run, "step", {"step_key": step.step_key, "kind": step.kind, "status": "failed", "code": error.code})
    _fail_run(run, error.code, error.message, step=step)
    return False


def _finish_step(run: WorkflowRun, step: WorkflowStepRun, status: str, *, output: dict | None = None) -> None:
    step.status = status
    step.output = output
    step.error = None if status == "succeeded" else step.error
    step.finished_at = timezone.now()
    step.resume_at = None
    step.save()
    _record_step_context(run, step)
    if status == "succeeded":
        audit.record("automation.step.succeeded", target=step, actor=("user", run.actor_id),
                     metadata={"run_id": str(run.id), "kind": step.kind, "attempt": step.attempt})
    _job_event(run, "step", {"step_key": step.step_key, "kind": step.kind, "status": status})


def _record_step_context(run: WorkflowRun, step: WorkflowStepRun) -> None:
    ctx = dict(run.context or {})
    steps = dict(ctx.get("steps") or {})
    steps[step.step_key] = {"status": step.status, "output": step.output or {}}
    ctx["steps"] = steps
    run.context = ctx
    run.save(update_fields=["context", "updated_at"])


# ------------------------------------------------------------------------------------------------ resume / cancel
def resume_step(step: WorkflowStepRun, *, status: str, output: dict | None = None, error: dict | None = None,
                reason: str = "") -> WorkflowRun:
    """Finish a paused step (approval decided, wait elapsed) or re-arm a retrying one, then re-queue its run. Inside
    the org's tenant context; the run row is locked."""
    run = WorkflowRun.objects.select_for_update().get(id=step.run_id)
    step.refresh_from_db()
    if run.status in WorkflowRun.TERMINAL or step.status not in ("waiting", "waiting_approval", "retrying"):
        return run
    if status == "pending":  # retry backoff elapsed
        step.status = "pending"
        step.resume_at = None
        step.save()
    elif status == "succeeded":
        _finish_step(run, step, "succeeded", output=output)
    else:
        step.status = "failed"
        step.error = error or {"code": reason or "step_failed", "message": reason}
        step.finished_at = timezone.now()
        step.save()
        _record_step_context(run, step)
        _job_event(run, "step", {"step_key": step.step_key, "status": "failed"})
        err = step.error or {}
        _fail_run(run, str(err.get("code") or "step_failed"), str(err.get("message") or ""), step=step)
        return run
    run.status = "queued"
    run.save(update_fields=["status", "updated_at"])
    enqueue(run)
    return run


def cancel_run(run: WorkflowRun, *, request=None) -> WorkflowRun:
    run = WorkflowRun.objects.select_for_update().get(id=run.id)
    if run.status in WorkflowRun.TERMINAL:
        return run
    now = timezone.now()
    pending_approvals = []
    for s in WorkflowStepRun.objects.filter(run=run).exclude(status__in=WorkflowStepRun.TERMINAL):
        if s.approval_id:
            pending_approvals.append(s.approval_id)
        s.status = "cancelled"
        s.finished_at = now
        s.save(update_fields=["status", "finished_at", "updated_at"])
    if pending_approvals:
        from .models import ApprovalRequest

        ApprovalRequest.objects.filter(id__in=pending_approvals, status="pending").update(
            status="cancelled", decided_at=now, updated_at=now)
    run.status = "cancelled"
    run.finished_at = now
    run.save(update_fields=["status", "finished_at", "updated_at"])
    audit.record("automation.run.cancel", target=run, request=request)
    if run.job_id:
        from apps.core.models import Job

        Job.objects.filter(id=run.job_id).update(status="cancelled", finished_at=now)
        jobs.publish(run.job_id, "done", {"status": "cancelled"})
    return run


# ------------------------------------------------------------------------------------------------ job events
def _job_update(run: WorkflowRun, status: str, message: str) -> None:
    if run.job_id:
        jobs.update(run.job_id, status=status, message=message)


def _job_event(run: WorkflowRun, event: str, data: dict) -> None:
    if run.job_id:
        jobs.publish(run.job_id, event, {"run_id": str(run.id), **data})
