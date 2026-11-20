"""Semantic layer API, model-centric routes (API_SPECIFICATION Phase 4; used by the dataset "Semantic" tab).

GET    /semantic-models?dataset_id=                    datasets.view   (models of datasets the caller may read;
                                                                         with dataset_id the model is generated on
                                                                         first access, like the dataset routes)
POST   /semantic-models                                semantic.manage ({dataset_id, name?}; 201 created / 200)
GET    /semantic-models/{id}                           datasets.view
PATCH  /semantic-models/{id}                           semantic.manage ({name?, description?, tables?: [...]})
POST   /semantic-models/{id}/generate                  semantic.manage ({reset?}) regenerate from the profile
POST   /semantic-models/{id}/publish                   semantic.manage (re-validates and builds the schema card)
GET    /semantic-models/{id}/metrics                   datasets.view
POST   /semantic-models/{id}/metrics                   semantic.manage
GET    /semantic-models/{id}/metrics/{metric_id}       datasets.view
PATCH  /semantic-models/{id}/metrics/{metric_id}       semantic.manage
DELETE /semantic-models/{id}/metrics/{metric_id}       semantic.manage

These are thin adapters over the same services as the dataset-scoped routes in `views.py` (W2 as-built contract):
one model per dataset, kept in sync with the current version and used live by Ask AI. Dataset ACLs apply on top of
permissions (`read` for reads, `admin` for writes); a model of a dataset the caller cannot see is a 404.

UI shape: tables carry `alias` (= logical table name) and columns `column_name` (= column name); `status` is
"published" while the model is ready (it is what Ask AI uses) and "failed" when the last refresh failed.
PATCH takes the UI's table list and applies ONLY the fields that changed, so unchanged values are never marked as
user edits (user edits survive refreshes). The UI role "attribute" is accepted as "dimension".
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core import audit
from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import services
from .generation import DEFAULT_AGGS, ROLES
from .models import MetricDefinition, SemanticModel
from .views import MetricCreateSerializer, MetricUpdateSerializer, RefreshSerializer

ROLE_ALIASES = {"attribute": "dimension"}
LIST_MAX = 500


def _dataset_for(request, dataset_id, privilege: str):
    from apps.datasets import services as datasets
    from apps.governance.services import check_dataset_access

    dataset = datasets.get_dataset(dataset_id)
    if not check_dataset_access(request.user.pk, dataset.id, privilege):
        raise NotFound()
    return dataset


def _model(request, pk, privilege: str) -> SemanticModel:
    model = SemanticModel.objects.filter(id=pk).first()
    if model is None:
        raise NotFound()
    _dataset_for(request, model.dataset_id, privilege)  # 404 for a deleted or invisible dataset
    return model


def ui_payload(model: SemanticModel) -> dict[str, Any]:
    body = services.model_payload(model)
    for table in body["tables"]:
        table["alias"] = table["name"]
        for col in table["columns"]:
            col["column_name"] = col["name"]
    body["model_status"] = body["status"]
    body["status"] = "published" if model.status == "ready" else model.status
    return body


def summary_payload(model: SemanticModel) -> dict[str, Any]:
    from apps.datasets.models import DatasetVersion

    version = DatasetVersion.objects.filter(id=model.dataset_version_id).values_list("version", flat=True).first()
    return {
        "id": str(model.id), "dataset_id": str(model.dataset_id), "name": model.name,
        "description": model.description, "version": version,
        "status": "published" if model.status == "ready" else model.status, "model_status": model.status,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
    }


# ------------------------------------------------------------------------------------------------ serializers
class UiColumnSerializer(serializers.Serializer):
    column_name = serializers.CharField(max_length=63)
    display_name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)
    role = serializers.ChoiceField(choices=[*ROLES, *ROLE_ALIASES], required=False)
    default_agg = serializers.ChoiceField(choices=list(DEFAULT_AGGS), required=False, allow_null=True)
    exposed_to_ai = serializers.BooleanField(required=False)


class UiTableSerializer(serializers.Serializer):
    alias = serializers.CharField(max_length=63)
    display_name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    synonyms = serializers.ListField(child=serializers.CharField(max_length=100, allow_blank=True), required=False)
    columns = UiColumnSerializer(many=True, required=False)


class UiModelPatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    tables = UiTableSerializer(many=True, required=False)


class UiModelCreateSerializer(serializers.Serializer):
    dataset_id = serializers.UUIDField()
    name = serializers.CharField(max_length=200, required=False)


def ui_patch_to_update(model: SemanticModel, data: dict[str, Any]) -> dict[str, Any]:
    """Translate the UI's full table list into `services.update_model` data containing ONLY changed fields."""
    current = {t["name"]: t for t in services.model_payload(model)["tables"]}
    out: dict[str, Any] = {"tables": [], "columns": []}
    if "description" in data and data["description"] != model.description:
        out["description"] = data["description"]
    for table in data.get("tables") or ():
        alias = table["alias"]
        cur = current.get(alias)
        if cur is None:
            raise ValidationError({"tables": [f"Unknown table {alias!r}."]})
        changed = {f: table[f] for f in ("display_name", "description", "synonyms")
                   if f in table and table[f] != cur.get(f)}
        if changed:
            out["tables"].append({"name": alias, **changed})
        cols = {c["name"]: c for c in cur["columns"]}
        for col in table.get("columns") or ():
            name = col["column_name"]
            ccur = cols.get(name)
            if ccur is None:
                raise ValidationError({"tables": [f"Unknown column {name!r} in {alias!r}."]})
            item = dict(col)
            if "role" in item:
                item["role"] = ROLE_ALIASES.get(item["role"], item["role"])
            cchanged = {f: item[f] for f in ("display_name", "description", "synonyms", "role", "default_agg",
                                             "exposed_to_ai") if f in item and item[f] != ccur.get(f)}
            if cchanged:
                out["columns"].append({"name": name, "table": alias, **cchanged})
    return out


# ------------------------------------------------------------------------------------------------ views
class SemanticModelListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "post": "semantic.manage"}

    def get(self, request, *args, **kwargs):
        raw = request.query_params.get("dataset_id")
        if raw:
            try:
                dataset_id = UUID(raw)
            except ValueError as exc:
                raise ValidationError({"dataset_id": ["Must be a UUID."]}) from exc
            dataset = _dataset_for(request, dataset_id, "read")
            from apps.datasets import services as datasets

            if datasets.current_version(dataset) is None or dataset.current_version.status != "ready":
                return Response([])  # no ready version yet: no model
            return Response([summary_payload(services.ensure_model(dataset))])
        from apps.governance.services import visible_dataset_ids

        models = list(SemanticModel.objects.exclude(dataset__status="deleted").order_by("-updated_at")[:LIST_MAX])
        visible = visible_dataset_ids(request.user.pk, [m.dataset_id for m in models], "read")
        return Response([summary_payload(m) for m in models if str(m.dataset_id) in visible])

    def post(self, request, *args, **kwargs):
        ser = UiModelCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        dataset = _dataset_for(request, ser.validated_data["dataset_id"], "admin")
        existed = SemanticModel.objects.filter(dataset_id=dataset.id).exists()
        model = services.ensure_model(dataset)
        name = (ser.validated_data.get("name") or "").strip()
        if name and name != model.name:
            _rename(model, name, request)
        if not existed:
            audit.record("semantic.model.create", target=model, request=request,
                         metadata={"dataset_id": str(dataset.id), "name": model.name})
        return Response(ui_payload(model), status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED)


def _rename(model: SemanticModel, name: str, request) -> None:
    before = model.name
    model.name = name[:200]
    model.updated_by = request.user.pk
    model.save(update_fields=["name", "updated_by", "updated_at"])
    audit.record("semantic.model.update", target=model, request=request,
                 changes={"before": {"name": before}, "after": {"name": model.name}})


class SemanticModelDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "patch": "semantic.manage"}

    def get(self, request, pk, *args, **kwargs):
        return Response(ui_payload(_model(request, pk, "read")))

    def patch(self, request, pk, *args, **kwargs):
        model = _model(request, pk, "admin")
        ser = UiModelPatchSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        with transaction.atomic():
            if data.get("name") and data["name"].strip() != model.name:
                _rename(model, data["name"].strip(), request)
            update = ui_patch_to_update(model, data)
            if update.get("tables") or update.get("columns") or "description" in update:
                services.update_model(model, update, user_id=request.user.pk, request=request)
        model.refresh_from_db()
        return Response(ui_payload(model))


class SemanticModelGenerateView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "semantic.manage"

    def post(self, request, pk, *args, **kwargs):
        model = _model(request, pk, "admin")
        ser = RefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        dataset = _dataset_for(request, model.dataset_id, "admin")
        model = services.refresh_model(dataset, reset=ser.validated_data["reset"], user_id=request.user.pk,
                                       request=request)
        return Response(ui_payload(model))


class SemanticModelPublishView(TenantAPIView):
    """Re-validates the model against the current version and builds the schema card Ask AI uses."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "semantic.manage"

    def post(self, request, pk, *args, **kwargs):
        model = _model(request, pk, "admin")
        dataset = _dataset_for(request, model.dataset_id, "admin")
        model = services.ensure_model(dataset)
        card = services.dataset_semantics(dataset.id)
        audit.record("semantic.model.publish", target=model, request=request, metadata={
            "dataset_version_id": str(card.dataset_version_id), "schema_hash": card.schema_hash,
            "metrics": len(card.metrics)})
        model.refresh_from_db()
        return Response(ui_payload(model))


class SemanticModelMetricListView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "post": "semantic.manage"}

    def get(self, request, pk, *args, **kwargs):
        model = _model(request, pk, "read")
        return Response([services.metric_payload(m) for m in model.metrics.order_by("created_at")])

    def post(self, request, pk, *args, **kwargs):
        model = _model(request, pk, "admin")
        data = {k: v for k, v in request.data.items() if not (k in ("key", "id") and not v)}  # UI sends key: ""
        ser = MetricCreateSerializer(data=data)
        ser.is_valid(raise_exception=True)
        metric = services.create_metric(model, ser.validated_data, user_id=request.user.pk, request=request)
        return Response(services.metric_payload(metric), status=status.HTTP_201_CREATED)


class SemanticModelMetricDetailView(TenantAPIView):
    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "patch": "semantic.manage", "delete": "semantic.manage"}

    def _metric(self, request, pk, metric_id, privilege: str) -> MetricDefinition:
        model = _model(request, pk, privilege)
        metric = model.metrics.filter(id=metric_id).first()
        if metric is None:
            raise NotFound()
        return metric

    def get(self, request, pk, metric_id, *args, **kwargs):
        return Response(services.metric_payload(self._metric(request, pk, metric_id, "read")))

    def patch(self, request, pk, metric_id, *args, **kwargs):
        metric = self._metric(request, pk, metric_id, "admin")
        ser = MetricUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        return Response(services.metric_payload(services.update_metric(metric, ser.validated_data, request=request)))

    def delete(self, request, pk, metric_id, *args, **kwargs):
        services.delete_metric(self._metric(request, pk, metric_id, "admin"), request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)
