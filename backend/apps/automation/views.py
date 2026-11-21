"""Automation API (API_SPECIFICATION Phase 10, CONTRACT "W5 additions"). All routes are under /api/v1/orgs/{org}/.

Reading (GET) workflows, runs, agents and the step catalog needs `automation.view`; changing workflows, triggers and
agents needs `automation.manage`; starting/cancelling runs needs `automation.run`. Approvals are visible to members
holding the approval's `approver_permission` (and to the requester); others get 404.

Step inputs/outputs can carry query results computed under the run actor's data policy, so they are shown only to
the actor and to members holding `audit.view`; everyone else sees `output_redacted: true`.
"""

from __future__ import annotations

from uuid import UUID

from django.db.models import Q
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import DenyApiKey, HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import agents, runner, services
from .models import AgentConfig, ApprovalRequest, Workflow, WorkflowRun, WorkflowTrigger

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
VIEW, MANAGE, RUN = "automation.view", "automation.manage", "automation.run"


class Pagination(DefaultCursorPagination):
    pass


# ------------------------------------------------------------------------------------------------ serializers
class WorkflowCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    description = serializers.CharField(required=False, allow_blank=True, default="", max_length=4000)
    graph = serializers.JSONField(required=False)


class WorkflowUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True, max_length=4000)
    status = serializers.ChoiceField(choices=["draft", "active", "paused", "archived"], required=False)
    graph = serializers.JSONField(required=False)


class GraphSerializer(serializers.Serializer):
    graph = serializers.JSONField()


class PublishSerializer(serializers.Serializer):
    version = serializers.IntegerField(required=False, min_value=1)


class TriggerCreateSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=["manual", "schedule", "webhook", "event"])
    config = serializers.JSONField(required=False, default=dict)


class TriggerUpdateSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()


class RunCreateSerializer(serializers.Serializer):
    input = serializers.JSONField(required=False, default=dict)

    def validate_input(self, value):
        if not isinstance(value, dict):
            raise ValidationError("Must be an object.")
        return value


class AgentRunSerializer(serializers.Serializer):
    goal = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)
    steps = serializers.JSONField(required=False)


class DecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["approve", "reject"])
    comment = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)
    password = serializers.CharField(required=False, allow_blank=True, default="", trim_whitespace=False)


# ------------------------------------------------------------------------------------------------ helpers
def _workflow(pk) -> Workflow:
    wf = Workflow.objects.select_related("current_version").filter(id=pk).first()
    if wf is None:
        raise NotFound()
    return wf


def _can_see_outputs(request, run: WorkflowRun) -> bool:
    return request.user.pk == run.actor_id or request.tenant.has_perm("audit.view")


def _run_body(request, run: WorkflowRun, *, with_steps: bool = False) -> dict:
    body = services.run_payload(run, with_steps=with_steps)
    if not _can_see_outputs(request, run):
        body["input"] = None
        body["output_redacted"] = True
        for s in body.get("steps", []):
            s["input"] = s["output"] = None
            s["output_redacted"] = True
    return body


def _paginate(view, request, qs, fn):
    paginator = Pagination()
    page = paginator.paginate_queryset(qs, request, view=view)
    return paginator.get_paginated_response([fn(x) for x in page])


# ------------------------------------------------------------------------------------------------ workflows
class WorkflowListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "post": MANAGE}

    def get(self, request, org):
        qs = Workflow.objects.select_related("current_version")
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        return _paginate(self, request, qs, services.workflow_payload)

    def post(self, request, org):
        ser = WorkflowCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        wf = services.create_workflow(owner_id=request.user.pk, request=request, **ser.validated_data)
        return Response(services.workflow_payload(wf, with_graph=True), status=status.HTTP_201_CREATED)


class WorkflowDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "*": MANAGE}

    def get(self, request, org, pk):
        return Response(services.workflow_payload(_workflow(pk), with_graph=True))

    def patch(self, request, org, pk):
        wf = _workflow(pk)
        ser = WorkflowUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        wf = services.update_workflow(wf, data=ser.validated_data, request=request)
        return Response(services.workflow_payload(wf, with_graph=True))

    def delete(self, request, org, pk):
        services.delete_workflow(_workflow(pk), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkflowVersionsView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "post": MANAGE}

    def get(self, request, org, pk):
        wf = _workflow(pk)
        return Response({"items": [services.version_payload(v) for v in wf.versions.order_by("-version")]})

    def post(self, request, org, pk):
        wf = _workflow(pk)
        ser = GraphSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = services.save_version(wf, ser.validated_data["graph"], user_id=request.user.pk, request=request)
        return Response(services.version_payload(v), status=status.HTTP_201_CREATED)


class WorkflowValidateView(TenantAPIView):
    """POST {graph} -> {valid, errors} (dry run, nothing saved)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = MANAGE

    def post(self, request, org):
        ser = GraphSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        from . import graph as graphmod

        errors = graphmod.validate(ser.validated_data["graph"])
        return Response({"valid": not errors, "errors": errors})


class WorkflowPublishView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = MANAGE

    def post(self, request, org, pk):
        wf = _workflow(pk)
        ser = PublishSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        services.publish(wf, version=ser.validated_data.get("version"), request=request)
        return Response(services.workflow_payload(wf, with_graph=True))


# ------------------------------------------------------------------------------------------------ triggers
class TriggerListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "post": MANAGE}

    def get(self, request, org, pk):
        wf = _workflow(pk)
        return Response({"items": [services.trigger_payload(t) for t in wf.triggers.order_by("created_at")]})

    def post(self, request, org, pk):
        wf = _workflow(pk)
        ser = TriggerCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        t, secret = services.create_trigger(wf, ttype=ser.validated_data["type"],
                                            config=ser.validated_data.get("config"), user_id=request.user.pk,
                                            request=request)
        return Response(services.trigger_payload(t, secret=secret), status=status.HTTP_201_CREATED)


def _trigger(pk, trigger_id) -> WorkflowTrigger:
    t = WorkflowTrigger.objects.filter(id=trigger_id, workflow_id=pk).first()
    if t is None:
        raise NotFound()
    return t


class TriggerDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "*": MANAGE}

    def get(self, request, org, pk, trigger_id):
        return Response(services.trigger_payload(_trigger(pk, trigger_id)))

    def patch(self, request, org, pk, trigger_id):
        t = _trigger(pk, trigger_id)
        ser = TriggerUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(services.trigger_payload(services.set_trigger_active(
            t, ser.validated_data["is_active"], request=request)))

    def delete(self, request, org, pk, trigger_id):
        services.delete_trigger(_trigger(pk, trigger_id), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class TriggerRotateSecretView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = MANAGE

    def post(self, request, org, pk, trigger_id):
        t = _trigger(pk, trigger_id)
        secret = services.rotate_secret(t, request=request)
        return Response(services.trigger_payload(t, secret=secret))


# ------------------------------------------------------------------------------------------------ runs
class WorkflowRunsView(TenantAPIView):
    """GET runs of one workflow (automation.view) · POST {input?} starts a manual run (automation.run) -> 202."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "post": RUN}

    def get(self, request, org, pk):
        wf = _workflow(pk)
        qs = WorkflowRun.objects.select_related("workflow", "version").filter(workflow=wf)
        return _paginate(self, request, qs, lambda r: _run_body(request, r))

    def post(self, request, org, pk):
        wf = _workflow(pk)
        ser = RunCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run = services.start_run(wf, actor_id=request.user.pk, run_input=ser.validated_data["input"],
                                 request=request)
        return Response(_accepted(org, run), status=status.HTTP_202_ACCEPTED)


def _accepted(org: str, run: WorkflowRun) -> dict:
    return {"run_id": str(run.id), "status": run.status, "job_id": str(run.job_id) if run.job_id else None,
            "status_url": f"/api/v1/orgs/{org}/workflow-runs/{run.id}",
            "events_url": f"/api/v1/orgs/{org}/jobs/{run.job_id}/events" if run.job_id else None}


class RunListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = VIEW

    def get(self, request, org):
        qs = WorkflowRun.objects.select_related("workflow", "version")
        params = request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        for key in ("workflow_id", "agent_id"):
            if params.get(key):
                try:
                    qs = qs.filter(**{key: UUID(params[key])})
                except ValueError as exc:
                    raise ValidationError({key: ["Must be a UUID."]}) from exc
        return _paginate(self, request, qs, lambda r: _run_body(request, r))


def _run(pk) -> WorkflowRun:
    run = WorkflowRun.objects.select_related("workflow", "version").filter(id=pk).first()
    if run is None:
        raise NotFound()
    return run


class RunDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = VIEW

    def get(self, request, org, pk):
        return Response(_run_body(request, _run(pk), with_steps=True))


class RunStepsView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = VIEW

    def get(self, request, org, pk):
        return Response({"items": _run_body(request, _run(pk), with_steps=True)["steps"]})


class RunCancelView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = RUN

    def post(self, request, org, pk):
        run = runner.cancel_run(_run(pk), request=request)
        return Response(_run_body(request, run, with_steps=True))


class StepCatalogView(TenantAPIView):
    """GET the registered step kinds with their config schemas (for the workflow builder and agent tools)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = VIEW

    def get(self, request, org):
        return Response({"items": services.step_catalog(), "events": list(services.EVENT_KINDS)})


# ------------------------------------------------------------------------------------------------ agents
def _agent(pk) -> AgentConfig:
    agent = AgentConfig.objects.filter(id=pk).first()
    if agent is None:
        raise NotFound()
    return agent


class AgentListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "post": MANAGE}

    def get(self, request, org):
        return _paginate(self, request, AgentConfig.objects.all(), agents.agent_payload)

    def post(self, request, org):
        if not isinstance(request.data, dict):
            raise ValidationError({"body": ["Must be an object."]})
        agent = agents.create_agent(data=dict(request.data), owner_id=request.user.pk, request=request)
        return Response(agents.agent_payload(agent), status=status.HTTP_201_CREATED)


class AgentDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": VIEW, "*": MANAGE}

    def get(self, request, org, pk):
        return Response(agents.agent_payload(_agent(pk)))

    def patch(self, request, org, pk):
        if not isinstance(request.data, dict):
            raise ValidationError({"body": ["Must be an object."]})
        agent = agents.update_agent(_agent(pk), data=dict(request.data), request=request)
        return Response(agents.agent_payload(agent))

    def delete(self, request, org, pk):
        agents.delete_agent(_agent(pk), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AgentRunsView(TenantAPIView):
    """POST {goal?, steps?: [{tool, config}]} -> 202 run. Without `steps`, Gemini plans from the goal."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = RUN

    def post(self, request, org, pk):
        agent = _agent(pk)
        ser = AgentRunSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run = agents.start_agent_run(agent, user_id=request.user.pk, info=request.tenant,
                                     goal=ser.validated_data.get("goal") or "",
                                     steps=ser.validated_data.get("steps"), request=request)
        return Response(_accepted(org, run), status=status.HTTP_202_ACCEPTED)


# ------------------------------------------------------------------------------------------------ approvals
class ApprovalListView(TenantAPIView):
    """GET ?status=pending|approved|rejected|expired|cancelled&mine=1. Members see approvals they may decide (they
    hold the approver permission) plus the ones they requested."""

    permission_classes = [IsAuthenticated, HasOrgPermission]

    def get(self, request, org):
        qs = services.visible_approvals(request.tenant, request.user.pk)
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        if request.query_params.get("mine") in ("1", "true"):
            qs = qs.filter(requested_by=request.user.pk)
        return _paginate(self, request, qs, services.approval_payload)


def _approval(request, pk) -> ApprovalRequest:
    a = ApprovalRequest.objects.filter(
        Q(approver_permission__in=sorted(request.tenant.permissions)) | Q(requested_by=request.user.pk),
        id=pk).first()
    if a is None:
        raise NotFound()
    return a


class ApprovalDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]

    def get(self, request, org, pk):
        a = _approval(request, pk)
        body = services.approval_payload(a)
        body["can_decide"] = a.status == "pending" and request.tenant.has_perm(a.approver_permission)
        return Response(body)


class ApprovalDecisionView(TenantAPIView):
    """POST {decision: approve|reject, comment?, password?}; `password` is required for impact=high. Interactive
    users only (API keys cannot decide)."""

    permission_classes = [IsAuthenticated, HasOrgPermission, DenyApiKey]

    def post(self, request, org, pk):
        a = ApprovalRequest.objects.filter(id=pk).first()
        if a is None or not request.tenant.has_perm(a.approver_permission):
            raise NotFound()
        ser = DecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        a = services.decide(a, user=request.user, info=request.tenant, decision=ser.validated_data["decision"],
                            comment=ser.validated_data["comment"], password=ser.validated_data["password"] or None,
                            request=request)
        return Response(services.approval_payload(a))
