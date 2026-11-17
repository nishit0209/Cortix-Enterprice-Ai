from __future__ import annotations

from django.db.models import Q
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView, TenantGenericViewSet

from . import services
from .exceptions import UploadRejected, UploadTooLarge
from .models import Dataset, DatasetColumn, DatasetVersion, Upload
from .serializers import (
    ClassificationSerializer,
    ColumnSerializer,
    DatasetCreateSerializer,
    DatasetSerializer,
    DatasetUpdateSerializer,
    NewVersionSerializer,
    UploadSerializer,
    VersionSerializer,
)

MULTIPART_OVERHEAD = 64 * 1024


def job_links(request, job) -> dict:
    base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


class UploadViewSet(mixins.RetrieveModelMixin, TenantGenericViewSet):
    """POST /uploads (multipart `file`) -> 201 {id, filename, size_bytes, status}; GET /uploads/{id}."""

    serializer_class = UploadSerializer
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"create": "datasets.create", "retrieve": "datasets.view"}
    parser_classes = [MultiPartParser, FormParser]

    def get_queryset(self):
        return Upload.objects.all()

    def create(self, request, *args, **kwargs):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > services.max_upload_bytes() + MULTIPART_OVERHEAD:
            raise UploadTooLarge(f"The request is larger than the {services.max_upload_bytes()} byte limit.")
        fileobj = request.FILES.get("file")
        if fileobj is None:
            raise ValidationError({"file": ["A file is required (multipart field 'file')."]})
        if len(request.FILES.getlist("file")) > 1:
            raise UploadRejected("Upload one file per request.", code="upload_multiple_files")
        upload = services.create_upload(fileobj, user_id=request.user.pk, request=request)
        return Response(UploadSerializer(upload).data, status=status.HTTP_201_CREATED)


class DatasetViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, TenantGenericViewSet):
    serializer_class = DatasetSerializer
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {
        "list": "datasets.view",
        "retrieve": "datasets.view",
        "create": "datasets.create",
        "partial_update": "datasets.manage",
        "update": "datasets.manage",
        "destroy": "datasets.manage",
        "classification": "datasets.classify",
        "columns": "datasets.view",
        "preview": "datasets.view",
        "versions": "datasets.view",
        "*": "datasets.view",
    }

    def get_queryset(self):
        qs = Dataset.objects.exclude(status="deleted").select_related("current_version")
        if self.action == "list":
            params = self.request.query_params
            term = (params.get("search") or "").strip()
            if term:
                qs = qs.filter(
                    Q(name__icontains=term) | Q(description__icontains=term) | Q(slug__icontains=term)
                    | Q(tags__contains=[term])
                )
            for field in ("status", "classification"):
                if params.get(field):
                    qs = qs.filter(**{field: params[field]})
            if params.get("tag"):
                qs = qs.filter(tags__contains=[params["tag"]])
        return qs

    def get_object(self):
        return services.get_dataset(self.kwargs["pk"])

    def create(self, request, *args, **kwargs):
        ser = DatasetCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        classification = data.get("classification")
        if classification and classification != "internal" and not request.tenant.has_perm("datasets.classify"):
            self.permission_denied(request, message="Setting a classification requires datasets.classify.")
        dataset, job = services.create_dataset(
            name=data["name"], upload_id=data["upload_id"], classification=classification,
            attest=data.get("attest", False), description=data.get("description", ""), tags=data.get("tags"),
            options=data.get("options"), user_id=request.user.pk, request=request,
        )
        body = {"dataset": DatasetSerializer(dataset).data, **job_links(request, job)}
        return Response(body, status=status.HTTP_202_ACCEPTED)

    def partial_update(self, request, *args, **kwargs):
        dataset = self.get_object()
        ser = DatasetUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        if "classification" in request.data:
            raise ValidationError({"classification": ["Use POST /datasets/{id}/classification."]})
        services.update_dataset(dataset, dict(ser.validated_data), request=request)
        return Response(DatasetSerializer(services.get_dataset(dataset.id)).data)

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        services.soft_delete(self.get_object(), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def classification(self, request, *args, **kwargs):
        dataset = self.get_object()
        ser = ClassificationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        services.set_classification(
            dataset, ser.validated_data["classification"], attest=ser.validated_data.get("attest", False),
            user_id=request.user.pk, request=request,
        )
        return Response(DatasetSerializer(services.get_dataset(dataset.id)).data)

    def _version_param(self, request) -> int | None:
        raw = request.query_params.get("version")
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValidationError({"version": ["Must be an integer."]}) from exc

    @action(detail=True, methods=["get"])
    def columns(self, request, *args, **kwargs):
        dataset = self.get_object()
        version_no = self._version_param(request)
        if version_no is not None:
            version = DatasetVersion.objects.filter(dataset_id=dataset.id, version=version_no).first()
            if version is None:
                raise NotFound()
        else:
            version = dataset.current_version
        if version is None:
            return Response([])
        cols = DatasetColumn.objects.filter(version_id=version.id).order_by("ordinal")
        return Response(ColumnSerializer(cols, many=True).data)

    @action(detail=True, methods=["get"])
    def preview(self, request, *args, **kwargs):
        dataset = self.get_object()
        try:
            limit = int(request.query_params.get("limit", 100))
        except ValueError as exc:
            raise ValidationError({"limit": ["Must be an integer."]}) from exc
        if limit < 1 or limit > services.PREVIEW_MAX:
            raise ValidationError({"limit": [f"Must be between 1 and {services.PREVIEW_MAX}."]})
        return Response(services.preview(dataset, limit=limit, version_no=self._version_param(request)))

    @action(detail=True, methods=["get", "post"])
    def versions(self, request, *args, **kwargs):
        dataset = self.get_object()
        if request.method == "POST":
            if not request.tenant.has_perm("datasets.create"):
                self.permission_denied(request, message="Adding a version requires datasets.create.")
            ser = NewVersionSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            job = services.add_version(dataset, upload_id=ser.validated_data["upload_id"],
                                       options=ser.validated_data.get("options"), user_id=request.user.pk,
                                       request=request)
            return Response({"dataset": DatasetSerializer(dataset).data, **job_links(request, job)},
                            status=status.HTTP_202_ACCEPTED)
        versions = DatasetVersion.objects.filter(dataset_id=dataset.id).order_by("-version")
        return Response(VersionSerializer(versions, many=True).data)


class DataStatsView(TenantAPIView):
    """GET /orgs/{org}/data-stats"""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "datasets.view"

    def get(self, request, *args, **kwargs):
        return Response(services.data_stats())
