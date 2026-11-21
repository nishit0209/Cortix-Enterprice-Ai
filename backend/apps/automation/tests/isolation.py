"""Isolation-matrix factories for the automation routes (see backend/testing/isolation.py)."""

from __future__ import annotations

GRAPH = {"steps": [{"key": "hello", "kind": "notify", "config": {"title": "Isolation probe"}}]}


def _workflow(world):
    from apps.automation.models import Workflow, WorkflowVersion

    wf = world.resources.get("automation_workflow")
    if wf is None:
        with world.tenant():
            wf = Workflow.objects.create(name="Isolation probe", owner_id=world.owner.pk, status="active")
            v = WorkflowVersion.objects.create(workflow=wf, version=1, graph=GRAPH, created_by=world.owner.pk)
            wf.current_version = v
            wf.save(update_fields=["current_version"])
        world.resources["automation_workflow"] = wf
    return wf


def _workflow_kwargs(world) -> dict[str, str]:
    return {"pk": str(_workflow(world).id)}


def _trigger(world) -> dict[str, str]:
    from apps.automation.models import WorkflowTrigger

    t = world.resources.get("automation_trigger")
    if t is None:
        wf = _workflow(world)
        with world.tenant():
            t = WorkflowTrigger.objects.create(workflow=wf, type="manual", config={}, created_by=world.owner.pk)
        world.resources["automation_trigger"] = t
    return {"pk": str(t.workflow_id), "trigger_id": str(t.id)}


def _run(world) -> dict[str, str]:
    from apps.automation.models import WorkflowRun, WorkflowStepRun

    run = world.resources.get("automation_run")
    if run is None:
        wf = _workflow(world)
        with world.tenant():
            run = WorkflowRun.objects.create(workflow=wf, version=wf.current_version, actor_id=world.owner.pk,
                                             graph=GRAPH, status="succeeded")
            WorkflowStepRun.objects.create(run=run, step_key="hello", kind="notify", status="succeeded")
        world.resources["automation_run"] = run
    return {"pk": str(run.id)}


def _agent(world) -> dict[str, str]:
    from apps.automation.models import AgentConfig

    agent = world.resources.get("automation_agent")
    if agent is None:
        with world.tenant():
            agent = AgentConfig.objects.create(name="Isolation probe", owner_id=world.owner.pk,
                                               allowed_tools=["notify"], approval_required_for=["high"])
        world.resources["automation_agent"] = agent
    return {"pk": str(agent.id)}


def _approval(world) -> dict[str, str]:
    from apps.automation.models import ApprovalRequest

    a = world.resources.get("automation_approval")
    if a is None:
        with world.tenant():
            a = ApprovalRequest.objects.create(kind="isolation.probe", title="Isolation probe",
                                               approver_permission="approvals.decide", impact="low",
                                               requested_by=world.owner.pk)
        world.resources["automation_approval"] = a
    return {"pk": str(a.id)}


ISOLATION_FACTORIES = {
    "automation-workflow-detail": _workflow_kwargs,
    "automation-workflow-versions": _workflow_kwargs,
    "automation-workflow-publish": _workflow_kwargs,
    "automation-trigger-list": _workflow_kwargs,
    "automation-workflow-runs": _workflow_kwargs,
    "automation-trigger-detail": _trigger,
    "automation-trigger-rotate-secret": _trigger,
    "automation-run-detail": _run,
    "automation-run-steps": _run,
    "automation-run-cancel": _run,
    "automation-agent-detail": _agent,
    "automation-agent-runs": _agent,
    "automation-approval-detail": _approval,
    "automation-approval-decision": _approval,
}
