"""Semantic layer API (dataset scoped).

GET    /datasets/{dataset_id}/semantic-model               datasets.view   (generated on first access)
PATCH  /datasets/{dataset_id}/semantic-model               semantic.manage (descriptions, synonyms, roles, ...)
POST   /datasets/{dataset_id}/semantic-model/refresh       semantic.manage ({reset?: bool})
GET    /datasets/{dataset_id}/metrics                      datasets.view
POST   /datasets/{dataset_id}/metrics                      semantic.manage
GET    /datasets/{dataset_id}/metrics/{metric_id}          datasets.view
PATCH  /datasets/{dataset_id}/metrics/{metric_id}          semantic.manage
DELETE /datasets/{dataset_id}/metrics/{metric_id}          semantic.manage

Dataset ACLs apply on top of permissions: `read` for reads, `admin` for writes (owner/admin bypass).
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import services
from .generation import DEFAULT_AGGS, ROLES
from .models import MetricDefinition


def _dataset(request, dataset_id, privilege: str):
    from apps.datasets import services as datasets
    from apps.governance.services import check_dataset_access

    dataset = datasets.get_dataset(dataset_id)
    if not check_dataset_access(request.user.pk, dataset.id, privilege):
        raise NotFound()
    return dataset


class TablePatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=63)
    display_name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)


class ColumnPatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=63)
    table = serializers.CharField(max_length=63, required=False)
    display_name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)
    role = serializers.ChoiceField(choices=list(ROLES), required=False)
    default_agg = serializers.ChoiceField(choices=list(DEFAULT_AGGS), required=False, allow_null=True)
    exposed_to_ai = serializers.BooleanField(required=False)


class ModelPatchSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    tables = TablePatchSerializer(many=True, required=False)
    columns = ColumnPatchSerializer(many=True, required=False)


class RefreshSerializer(serializers.Serializer):
    reset = serializers.BooleanField(required=False, default=False)


class MetricCreateSerializer(serializers.Serializer):
    key = serializers.RegexField(r"^[a-z][a-z0-9_]{0,62}$", required=False)
    name = serializers.CharField(max_length=200)
    expression = serializers.CharField(max_length=2000)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False,
                                     default=list)


class MetricUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    expression = serializers.CharField(max_length=2000, required=False)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)
    is_active = serializers.BooleanField(required=False)


class SemanticModelView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "patch": "semantic.manage"}

    def get(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(request, dataset_id, "read")
        return Response(services.model_payload(services.ensure_model(dataset)))

    def patch(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(request, dataset_id, "admin")
        ser = ModelPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        model = services.ensure_model(dataset)
        services.update_model(model, ser.validated_data, user_id=request.user.pk, request=request)
        model.refresh_from_db()
        return Response(services.model_payload(model))


class SemanticModelRefreshView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "semantic.manage"

    def post(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(request, dataset_id, "admin")
        ser = RefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        model = services.refresh_model(dataset, reset=ser.validated_data["reset"], user_id=request.user.pk,
                                       request=request)
        return Response(services.model_payload(model))


class MetricListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "post": "semantic.manage"}

    def get(self, request, dataset_id, *args, **kwargs):
        model = services.ensure_model(_dataset(request, dataset_id, "read"))
        return Response([services.metric_payload(m) for m in model.metrics.order_by("created_at")])

    def post(self, request, dataset_id, *args, **kwargs):
        model = services.ensure_model(_dataset(request, dataset_id, "admin"))
        ser = MetricCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        metric = services.create_metric(model, ser.validated_data, user_id=request.user.pk, request=request)
        return Response(services.metric_payload(metric), status=status.HTTP_201_CREATED)


class MetricDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "patch": "semantic.manage", "delete": "semantic.manage"}

    def _metric(self, request, dataset_id, metric_id, privilege: str) -> MetricDefinition:
        model = services.ensure_model(_dataset(request, dataset_id, privilege))
        metric = model.metrics.filter(id=metric_id).first()
        if metric is None:
            raise NotFound()
        return metric

    def get(self, request, dataset_id, metric_id, *args, **kwargs):
        return Response(services.metric_payload(self._metric(request, dataset_id, metric_id, "read")))

    def patch(self, request, dataset_id, metric_id, *args, **kwargs):
        metric = self._metric(request, dataset_id, metric_id, "admin")
        ser = MetricUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        metric = services.update_metric(metric, ser.validated_data, request=request)
        return Response(services.metric_payload(metric))

    def delete(self, request, dataset_id, metric_id, *args, **kwargs):
        services.delete_metric(self._metric(request, dataset_id, metric_id, "admin"), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)
