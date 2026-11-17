from __future__ import annotations

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core import jobs
from apps.core.permissions import HasOrgPermission
from apps.core.tenancy import require_org_id
from apps.core.tenancy.views import TenantAPIView

from . import services
from .engine import RuleValidationError
from .models import RULE_TYPES, SEVERITIES, QualityRule


def _dataset(dataset_id):
    from apps.datasets import services as dataset_services

    return dataset_services.get_dataset(dataset_id)


def _column_types(dataset) -> dict[str, str]:
    from apps.datasets import services as dataset_services

    version = dataset.current_version
    if version is None:
        return {}
    return {c["name"]: c["data_type"] for c in dataset_services.version_columns(version)}


def _validation_error(exc: RuleValidationError) -> ValidationError:
    return ValidationError({k: [v] for k, v in exc.errors.items()})


class RuleCreateSerializer(serializers.Serializer):
    rule_type = serializers.ChoiceField(choices=[t for t, _ in RULE_TYPES])
    column = serializers.CharField(max_length=63, required=False, allow_null=True, allow_blank=True)
    params = serializers.DictField(required=False, default=dict)
    severity = serializers.ChoiceField(choices=[s for s, _ in SEVERITIES], required=False, default="warn")
    is_blocking = serializers.BooleanField(required=False, default=False)
    is_active = serializers.BooleanField(required=False, default=True)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")


class RuleUpdateSerializer(serializers.Serializer):
    column = serializers.CharField(max_length=63, required=False, allow_null=True, allow_blank=True)
    params = serializers.DictField(required=False)
    severity = serializers.ChoiceField(choices=[s for s, _ in SEVERITIES], required=False)
    is_blocking = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)
    description = serializers.CharField(max_length=300, required=False, allow_blank=True)


class DatasetQualityView(TenantAPIView):
    """GET /datasets/{id}/quality -> {score, dimensions, rules, duplicates}"""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "datasets.view"

    def get(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(dataset_id)
        version = dataset.current_version
        version_no = request.query_params.get("version")
        if version_no:
            try:
                version = dataset.versions.filter(version=int(version_no)).first()
            except ValueError as exc:
                raise ValidationError({"version": ["Must be an integer."]}) from exc
            if version is None:
                raise NotFound()
        return Response(services.report_payload(version))


class QualityEvaluateView(TenantAPIView):
    """POST /datasets/{id}/quality/evaluate -> 202 job (re-evaluate active rules on the current version)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "quality.manage"

    def post(self, request, dataset_id, *args, **kwargs):
        from apps.datasets.exceptions import DatasetNotReady

        from .tasks import evaluate_dataset_quality

        dataset = _dataset(dataset_id)
        version = dataset.current_version
        if version is None or version.status != "ready":
            raise DatasetNotReady()
        job = jobs.create_job("quality_evaluate", user_id=request.user.pk, message="Queued")
        kwargs = {"org_id": str(require_org_id()), "dataset_id": str(dataset.id), "version_id": str(version.id),
                  "job_id": str(job.id), "actor_id": str(request.user.pk)}
        transaction.on_commit(lambda: evaluate_dataset_quality.delay(**kwargs))
        base = f"/api/v1/orgs/{request.tenant.slug}/jobs/{job.id}"
        return Response({"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"},
                        status=status.HTTP_202_ACCEPTED)


class QualityRuleListView(TenantAPIView):
    """GET/POST /datasets/{id}/quality-rules"""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "post": "quality.manage"}

    def get(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(dataset_id)
        latest = services.latest_results(dataset.current_version_id) if dataset.current_version_id else {}
        rules = QualityRule.objects.filter(dataset_id=dataset.id).order_by("created_at")
        return Response([services.rule_payload(r, latest.get(str(r.id))) for r in rules])

    def post(self, request, dataset_id, *args, **kwargs):
        dataset = _dataset(dataset_id)
        ser = RuleCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        column_types = _column_types(dataset)
        if not column_types:
            from apps.datasets.exceptions import DatasetNotReady

            raise DatasetNotReady("Rules can be added once the dataset has a ready version.")
        try:
            rule = services.create_rule(dataset, column_types=column_types, data=ser.validated_data,
                                        user_id=request.user.pk, request=request)
        except RuleValidationError as exc:
            raise _validation_error(exc) from exc
        return Response(services.rule_payload(rule), status=status.HTTP_201_CREATED)


class QualityRuleDetailView(TenantAPIView):
    """GET/PATCH/DELETE /datasets/{id}/quality-rules/{rule_id}"""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = {"get": "datasets.view", "patch": "quality.manage", "delete": "quality.manage"}

    def _rule(self, dataset_id, rule_id):
        dataset = _dataset(dataset_id)
        rule = QualityRule.objects.filter(dataset_id=dataset.id, id=rule_id).first()
        if rule is None:
            raise NotFound()
        return dataset, rule

    def get(self, request, dataset_id, rule_id, *args, **kwargs):
        dataset, rule = self._rule(dataset_id, rule_id)
        latest = services.latest_results(dataset.current_version_id) if dataset.current_version_id else {}
        return Response(services.rule_payload(rule, latest.get(str(rule.id))))

    def patch(self, request, dataset_id, rule_id, *args, **kwargs):
        dataset, rule = self._rule(dataset_id, rule_id)
        ser = RuleUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        if "column" in data and not data["column"]:
            data["column"] = None
        try:
            rule = services.update_rule(rule, column_types=_column_types(dataset), data=data, request=request)
        except RuleValidationError as exc:
            raise _validation_error(exc) from exc
        return Response(services.rule_payload(rule))

    def delete(self, request, dataset_id, rule_id, *args, **kwargs):
        _, rule = self._rule(dataset_id, rule_id)
        services.delete_rule(rule, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)
