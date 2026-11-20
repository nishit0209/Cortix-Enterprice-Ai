from __future__ import annotations

from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantGenericViewSet

from . import services
from .models import DataSource, SourceImport
from .serializers import TABLE_MAX, DataSourceCreateSerializer, DataSourceUpdateSerializer, ImportSerializer


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


def import_payload(imp: SourceImport) -> dict:
    return {
        "id": str(imp.id), "table": imp.table_name, "schema": imp.schema_name, "columns": imp.columns,
        "mode": imp.mode, "cursor_column": imp.cursor_column or None, "dataset_id": str(imp.dataset_id)
        if imp.dataset_id else None, "dataset_name": imp.dataset_name, "classification": imp.classification,
        "status": imp.status, "rows": imp.rows, "job_id": str(imp.job_id) if imp.job_id else None,
        "ingest_job_id": str(imp.ingest_job_id) if imp.ingest_job_id else None, "error": imp.error,
        "created_at": imp.created_at.isoformat(), "finished_at": imp.finished_at.isoformat() if imp.finished_at else None,
    }


class DataSourceViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    """/data-sources: CRUD + POST test / introspect / import (202 jobs). The password is write-only."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "sources.view",
        "retrieve": "sources.view",
        "create": "sources.manage",
        "partial_update": "sources.manage",
        "update": "sources.manage",
        "destroy": "sources.manage",
        "test": "sources.manage",
        "introspect": "sources.view",  # POST additionally needs sources.manage (checked in the action)
        "import_table": "sources.manage",
        "imports": "sources.view",
        "*": "sources.view",
    }

    def get_queryset(self):
        return DataSource.objects.all().order_by("-created_at")

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([services.source_payload(s) for s in page])

    def retrieve(self, request, *args, **kwargs):
        return Response(services.source_payload(services.get_source(kwargs["pk"])))

    def create(self, request, *args, **kwargs):
        ser = DataSourceCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        source, job = services.create_source(
            name=d["name"], type=d["type"], config=d["config"], username=d["username"], password=d["password"],
            user_id=request.user.pk, request=request,
        )
        return Response({**services.source_payload(source), "test_job": job_links(request, job)},
                        status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        source = services.get_source(kwargs["pk"])
        ser = DataSourceUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        source, job = services.update_source(source, dict(ser.validated_data), user_id=request.user.pk,
                                             request=request)
        body = services.source_payload(source)
        if job is not None:
            body["test_job"] = job_links(request, job)
        return Response(body)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        services.delete_source(services.get_source(kwargs["pk"]), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def test(self, request, *args, **kwargs):
        source = services.get_source(kwargs["pk"])
        job = services.start_test(source, user_id=request.user.pk, request=request)
        return Response(job_links(request, job), status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get", "post"])
    def introspect(self, request, *args, **kwargs):
        """POST: 202 job that refreshes the catalog. GET: the last stored catalog (schemas -> tables -> columns)."""
        source = services.get_source(kwargs["pk"])
        if request.method == "GET":
            return Response({
                "introspected_at": source.introspected_at.isoformat() if source.introspected_at else None,
                "catalog": source.catalog or {"schemas": [], "table_count": 0, "truncated": False},
            })
        if not request.tenant.has_perm("sources.manage"):
            self.permission_denied(request, message="Refreshing the catalog requires sources.manage.")
        job = services.start_introspect(source, user_id=request.user.pk, request=request)
        return Response(job_links(request, job), status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="import", url_name="import")
    def import_table(self, request, *args, **kwargs):
        source = services.get_source(kwargs["pk"])
        ser = ImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if not request.tenant.has_perm("datasets.create"):
            self.permission_denied(request, message="Importing creates a dataset and requires datasets.create.")
        classification = d.get("classification")
        if classification and classification != "internal" and not request.tenant.has_perm("datasets.classify"):
            self.permission_denied(request, message="Setting a classification requires datasets.classify.")
        schema, table = d.get("schema"), d["table"]
        if not schema:  # the catalog browser sends "schema.table"
            schema, table = services.split_qualified_table(source, table)
        if len(table) > TABLE_MAX:
            raise ValidationError({"table": [f"Ensure this field has no more than {TABLE_MAX} characters."]})
        imp, job = services.start_import(
            source, table=table, schema=schema, name=d["name"], columns=d.get("columns"),
            classification=classification, attest=d.get("attest", False), mode=d.get("mode", "full"),
            cursor_column=d.get("cursor_column"), dataset_id=d.get("dataset_id"), user_id=request.user.pk,
            request=request,
        )
        return Response({"import": import_payload(imp), **job_links(request, job)}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def imports(self, request, *args, **kwargs):
        source = services.get_source(kwargs["pk"])
        items = SourceImport.objects.filter(data_source_id=source.id).order_by("-created_at")[:100]
        return Response({"items": [import_payload(i) for i in items], "next_cursor": None, "previous_cursor": None})
