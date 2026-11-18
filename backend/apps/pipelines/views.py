from __future__ import annotations

from django.db.models import Q
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantGenericViewSet

from . import services
from .models import Pipeline, PipelineRun, PipelineVersion
from .serializers import (
    PipelineCreateSerializer,
    PipelineUpdateSerializer,
    PreviewSerializer,
    ScheduleSerializer,
    ValidateSerializer,
)


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


def _run_accepted(request, run, job) -> Response:
    body = {"run": services.run_summary(run), **job_links(request, job)}
    return Response(body, status=status.HTTP_202_ACCEPTED)


class PipelineViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    """/pipelines CRUD, runs, schedule, preview, versions, validate."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "pipelines.view",
        "retrieve": "pipelines.view",
        "create": "pipelines.manage",
        "partial_update": "pipelines.manage",
        "update": "pipelines.manage",
        "destroy": "pipelines.manage",
        "runs": "pipelines.view",          # POST additionally needs pipelines.run
        "schedule": "pipelines.view",      # PUT/DELETE additionally need pipelines.manage
        "preview": "pipelines.manage",
        "validate": "pipelines.manage",
        "versions": "pipelines.view",
        "*": "pipelines.view",
    }

    def get_queryset(self):
        qs = Pipeline.objects.select_related("current_version").order_by("-created_at")
        params = self.request.query_params
        if self.action == "list":
            st = params.get("status")
            qs = qs.filter(status=st) if st else qs.exclude(status="archived")
            term = (params.get("search") or "").strip()
            if term:
                qs = qs.filter(Q(name__icontains=term) | Q(description__icontains=term))
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([services.pipeline_payload(p) for p in page])

    def create(self, request, *args, **kwargs):
        ser = PipelineCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        pipeline = services.create_pipeline(
            name=d["name"], dag=d.get("dag"), description=d.get("description", ""), max_retries=d.get("max_retries"),
            retry_backoff_s=d.get("retry_backoff_s"), notify_on_failure=d.get("notify_on_failure", True),
            user_id=request.user.pk, request=request,
        )
        return Response(services.pipeline_payload(pipeline), status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        return Response(services.pipeline_payload(services.get_pipeline(kwargs["pk"])))

    def partial_update(self, request, *args, **kwargs):
        pipeline = services.get_pipeline(kwargs["pk"])
        ser = PipelineUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        services.update_pipeline(pipeline, dict(ser.validated_data), user_id=request.user.pk, request=request)
        return Response(services.pipeline_payload(services.get_pipeline(pipeline.id)))

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        services.archive_pipeline(services.get_pipeline(kwargs["pk"]), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "post"])
    def runs(self, request, *args, **kwargs):
        pipeline = services.get_pipeline(kwargs["pk"])
        if request.method == "POST":
            if not request.tenant.has_perm("pipelines.run"):
                self.permission_denied(request, message="Running pipelines requires pipelines.run.")
            run, job = services.trigger_run(pipeline, user_id=request.user.pk, request=request)
            return _run_accepted(request, run, job)
        qs = PipelineRun.objects.filter(pipeline_id=pipeline.id).select_related("pipeline").order_by("-created_at")
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response([services.run_summary(r) for r in page])

    @action(detail=True, methods=["get", "put", "delete"])
    def schedule(self, request, *args, **kwargs):
        pipeline = services.get_pipeline(kwargs["pk"])
        if request.method == "GET":
            sched = pipeline.schedules.first()
            return Response(services.schedule_payload(sched) if sched else None)
        if not request.tenant.has_perm("pipelines.manage"):
            self.permission_denied(request, message="Scheduling pipelines requires pipelines.manage.")
        if request.method == "DELETE":
            services.delete_schedule(pipeline, request=request)
            return Response(status=status.HTTP_204_NO_CONTENT)
        ser = ScheduleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        sched = services.set_schedule(pipeline, cron=d["cron"], tz=d.get("timezone", "UTC"),
                                      enabled=d.get("enabled", True), user_id=request.user.pk, request=request)
        return Response(services.schedule_payload(sched))

    @action(detail=True, methods=["post"])
    def preview(self, request, *args, **kwargs):
        pipeline = services.get_pipeline(kwargs["pk"])
        ser = PreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        dag = ser.validated_data.get("dag")
        if dag is None:
            dag = pipeline.current_version.dag if pipeline.current_version else None
        if dag is None:
            from apps.core.exceptions import Conflict

            raise Conflict("The pipeline has no DAG to preview.", code="pipeline_has_no_version")
        return Response(services.preview(dag, user_id=request.user.pk, limit=ser.validated_data.get("limit", 200)))

    @action(detail=False, methods=["post"])
    def validate(self, request, *args, **kwargs):
        ser = ValidateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(services.validate(ser.validated_data["dag"], request.user.pk).payload())

    @action(detail=True, methods=["get"])
    def versions(self, request, *args, **kwargs):
        pipeline = services.get_pipeline(kwargs["pk"])
        rows = PipelineVersion.objects.filter(pipeline_id=pipeline.id).order_by("-version")[:200]
        return Response([{"id": str(v.id), "version": v.version, "dag": v.dag, "created_at": v.created_at.isoformat(),
                          "created_by": str(v.created_by) if v.created_by else None,
                          "current": v.id == pipeline.current_version_id} for v in rows])


class PipelineRunViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    """/pipeline-runs: list, detail (steps + logs), retry, cancel, logs."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "pipelines.view",
        "retrieve": "pipelines.view",
        "logs": "pipelines.view",
        "retry": "pipelines.run",
        "cancel": "pipelines.run",
        "*": "pipelines.view",
    }

    def get_queryset(self):
        qs = PipelineRun.objects.select_related("pipeline").order_by("-created_at")
        st = self.request.query_params.get("status")
        return qs.filter(status=st) if st else qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([services.run_summary(r) for r in page])

    def retrieve(self, request, *args, **kwargs):
        return Response(services.run_payload(services.get_run(kwargs["pk"])))

    @action(detail=True, methods=["get"])
    def logs(self, request, *args, **kwargs):
        run = services.get_run(kwargs["pk"])
        return Response({"items": services.run_logs(run, limit=5000), "next_cursor": None, "previous_cursor": None})

    @action(detail=True, methods=["post"])
    def retry(self, request, *args, **kwargs):
        run, job = services.retry_run(services.get_run(kwargs["pk"]), user_id=request.user.pk, request=request)
        return _run_accepted(request, run, job)

    @action(detail=True, methods=["post"])
    def cancel(self, request, *args, **kwargs):
        run = services.cancel_run(services.get_run(kwargs["pk"]), request=request)
        return Response(services.run_payload(run, include_logs=False))
