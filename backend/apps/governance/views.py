"""Governance REST endpoints (API_SPECIFICATION.md Phase 5), all `governance.policy.edit`, never via API keys.

    GET, POST, DELETE            /orgs/{org}/datasets/{id}/acl[/{acl_id}]
    GET, POST, PATCH, DELETE     /orgs/{org}/datasets/{id}/column-policies[/{policy_id}]
    GET, POST, PATCH, DELETE     /orgs/{org}/datasets/{id}/row-policies[/{policy_id}]
    POST                         /orgs/{org}/datasets/{id}/policy-preview
    GET, PUT                     /orgs/{org}/users/{user_id}/attributes
    GET                          /orgs/{org}/access-history   (`audit.view`)
    GET, POST                    /orgs/{org}/retention-policies           (`governance.retention.edit`)
    GET, PATCH, DELETE           /orgs/{org}/retention-policies/{id}
    POST                         /orgs/{org}/retention-policies/{id}/dry-run · /apply
    GET                          /orgs/{org}/retention-targets            supported targets
"""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from rest_framework import mixins, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.services import users_by_id
from apps.core import audit
from apps.core.exceptions import Conflict
from apps.core.pagination import DefaultCursorPagination
from apps.core.permissions import DenyApiKey, HasOrgPermission
from apps.core.registry import asset_exists, policy_sampler
from apps.core.tenancy.views import TenantAPIView, TenantGenericViewSet
from apps.orgs.models import Membership

from . import retention, services
from .models import AccessHistory, ColumnPolicy, DatasetAcl, RetentionPolicy, RowPolicy, UserAttribute
from .serializers import (
    ColumnPolicySerializer,
    DatasetAclSerializer,
    PolicyPreviewSerializer,
    RetentionPolicySerializer,
    RowPolicySerializer,
    UserAttributesSerializer,
)

UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
PERMS = [IsAuthenticated, DenyApiKey, HasOrgPermission]


def _check_dataset(dataset_id) -> None:
    if asset_exists("dataset", dataset_id) is False:  # None = cannot verify (datasets app not registered)
        raise NotFound()


class DatasetScopedViewSet(TenantGenericViewSet):
    permission_classes = PERMS
    required_permissions = "governance.policy.edit"
    lookup_value_regex = UUID_RE
    model = None
    audit_prefix = ""

    @property
    def dataset_id(self):
        return self.kwargs["dataset_id"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        _check_dataset(self.dataset_id)

    def get_queryset(self):
        return self.model.objects.filter(dataset_id=self.dataset_id).order_by("-created_at")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "dataset_id": self.dataset_id}

    def perform_create(self, serializer):
        obj = serializer.save(dataset_id=self.dataset_id, created_by=self.request.user.pk)
        audit.record(f"{self.audit_prefix}.create", target=obj, metadata=self._audit_meta(serializer.data),
                     request=self.request)

    def perform_update(self, serializer):
        before = self.get_serializer(serializer.instance).data
        obj = serializer.save()
        after = serializer.data
        changes = {k: [before.get(k), after.get(k)] for k in serializer.validated_data if before.get(k) != after.get(k)}
        audit.record(f"{self.audit_prefix}.update", target=obj, changes=changes,
                     metadata={"dataset_id": str(self.dataset_id)}, request=self.request)

    def perform_destroy(self, instance):
        audit.record(f"{self.audit_prefix}.delete", target=instance,
                     metadata=self._audit_meta(self.get_serializer(instance).data), request=self.request)
        instance.delete()

    @staticmethod
    def _audit_meta(data) -> dict:
        return {k: (str(v) if v is not None and not isinstance(v, (dict, list, bool, int)) else v)
                for k, v in dict(data).items() if k not in ("created_at", "updated_at")}


class DatasetAclViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin,
                        DatasetScopedViewSet):
    serializer_class = DatasetAclSerializer
    model = DatasetAcl
    audit_prefix = "governance.acl"

    def perform_create(self, serializer):
        v = serializer.validated_data
        if DatasetAcl.objects.filter(dataset_id=self.dataset_id, principal_type=v["principal_type"],
                                     principal_id=v["principal_id"], privilege=v["privilege"]).exists():
            raise Conflict("This grant already exists.", code="duplicate")
        super().perform_create(serializer)


class ColumnPolicyViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                          mixins.UpdateModelMixin, mixins.DestroyModelMixin, DatasetScopedViewSet):
    serializer_class = ColumnPolicySerializer
    model = ColumnPolicy
    audit_prefix = "governance.column_policy"


class RowPolicyViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                       mixins.UpdateModelMixin, mixins.DestroyModelMixin, DatasetScopedViewSet):
    serializer_class = RowPolicySerializer
    model = RowPolicy
    audit_prefix = "governance.row_policy"


class PolicyPreviewView(TenantAPIView):
    """Compute the effective policy AS a chosen member, optionally with a small sample query.

    Body: {user_id, tables?, columns_by_table?, privilege?, sample?: bool, limit?: 1..50}. Without tables/columns the
    dataset schema provider registered by the datasets app is used. A user that fails the ACL gets `access: false`.

    `sample: true` runs `SELECT <visible columns> ... LIMIT n` through the real guard and executor under the
    member's policy INTERSECTED with the caller's own policy (the caller never sees more than they may see
    themselves; no data leaves the org). The sample is audited (BI query audit purpose `policy_preview`, access
    history). The response adds `sample: {ok, columns, rows, row_count, truncated, executed_sql, query_audit_id}`
    or `sample: {ok: false, code, detail}`."""

    rollback_on_error = False  # a rejected sample keeps its audit row

    permission_classes = PERMS
    required_permissions = "governance.policy.edit"

    def post(self, request, org, dataset_id):
        _check_dataset(dataset_id)
        ser = PolicyPreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if not Membership.objects.filter(user_id=data["user_id"]).exists():
            raise NotFound()
        tables, columns = data.get("tables"), data.get("columns_by_table")
        try:
            policy = services.effective_policy(data["user_id"], dataset_id, tables, columns,
                                               privilege=data["privilege"])
        except services.DatasetAccessDenied:
            return Response({"access": False, "policy": None})
        except ValueError as exc:
            raise ValidationError({"tables": [str(exc)]}) from exc
        body = {"access": True, "policy": services.policy_to_dict(policy)}
        if data["sample"]:
            body["sample"] = self._sample(request, dataset_id, policy, data)
        return Response(body)

    @staticmethod
    def _sample(request, dataset_id, policy, data) -> dict:
        sampler = policy_sampler()
        if sampler is None:
            return {"ok": False, "code": "sample_unavailable", "detail": "Sampling is not available."}
        try:
            mine = services.effective_policy(request.user.pk, dataset_id, privilege="query")
        except services.DatasetAccessDenied:
            return {"ok": False, "code": "sample_not_permitted",
                    "detail": "You cannot query this dataset yourself, so no sample can be shown."}
        combined = services.intersect_policies(policy, mine)
        return sampler(combined, actor_id=request.user.pk, impersonated_user_id=data["user_id"], limit=data["limit"])


class UserAttributesView(TenantAPIView):
    """GET -> {user_id, attributes: {key: [values]}}; PUT {attributes} replaces them all."""

    permission_classes = PERMS
    required_permissions = "governance.policy.edit"

    def _require_member(self, user_id) -> None:
        if not Membership.objects.filter(user_id=user_id).exists():
            raise NotFound()

    @staticmethod
    def _body(user_id) -> dict:
        attrs = {a.key: list(a.values) for a in UserAttribute.objects.filter(user_id=user_id).order_by("key")}
        return {"user_id": str(user_id), "attributes": attrs}

    def get(self, request, org, user_id):
        self._require_member(user_id)
        return Response(self._body(user_id))

    def put(self, request, org, user_id):
        self._require_member(user_id)
        ser = UserAttributesSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new = ser.validated_data["attributes"]
        before = self._body(user_id)["attributes"]
        with transaction.atomic():
            UserAttribute.objects.filter(user_id=user_id).exclude(key__in=list(new)).delete()
            for key, values in new.items():
                UserAttribute.objects.update_or_create(user_id=user_id, key=key, defaults={"values": values})
        if before != new:
            audit.record("governance.user_attributes.update", target_type="accounts.user", target_id=str(user_id),
                         changes={"attributes": [before, new]}, request=request)
        return Response(self._body(user_id), status=status.HTTP_200_OK)


class AccessHistoryPagination(DefaultCursorPagination):
    ordering = ("-occurred_at", "-id")


class AccessHistoryView(TenantAPIView):
    """GET /orgs/{org}/access-history?asset_type=&asset_id=&user_id=&access_type= (perm `audit.view`).

    Append-only record of dataset queries/exports (written by apps.bi). Never contains data values."""

    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "audit.view"
    FILTERS = ("asset_type", "asset_id", "user_id", "access_type")

    def get(self, request, org):
        qs = AccessHistory.objects.all()
        for name in self.FILTERS:
            value = request.query_params.get(name)
            if not value:
                continue
            if name.endswith("_id"):
                try:
                    value = UUID(value)
                except ValueError as exc:
                    raise ValidationError({name: ["Must be a UUID."]}) from exc
            qs = qs.filter(**{name: value})
        paginator = AccessHistoryPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        users = users_by_id(r.user_id for r in page if r.user_id)
        return paginator.get_paginated_response([
            {
                "id": str(r.id),
                "occurred_at": r.occurred_at,
                "user_id": str(r.user_id) if r.user_id else None,
                "user_email": getattr(users.get(str(r.user_id)), "email", None),
                "api_key_id": str(r.api_key_id) if r.api_key_id else None,
                "asset_type": r.asset_type,
                "asset_id": str(r.asset_id),
                "access_type": r.access_type,
                "query_audit_id": str(r.query_audit_id) if r.query_audit_id else None,
                "metadata": r.metadata,
            }
            for r in page
        ])


# ================================================================================================ retention (W4)
class RetentionPolicyViewSet(TenantGenericViewSet):
    """Retention policies (`governance.retention.edit`, never via API keys). See apps/governance/retention.py.

    `mode` starts as `dry_run`; switching to `enforce` needs a dry run with the CURRENT window/action (409
    `retention_dry_run_required`). `POST /{id}/apply {confirm: true}` deletes/archives now (also needs a dry run)."""

    permission_classes = PERMS
    required_permissions = "governance.retention.edit"
    serializer_class = RetentionPolicySerializer
    lookup_value_regex = UUID_RE
    pagination_class = DefaultCursorPagination

    def get_queryset(self):
        return RetentionPolicy.objects.all()

    def _get(self, pk) -> RetentionPolicy:
        obj = RetentionPolicy.objects.filter(id=pk).first()
        if obj is None:
            raise NotFound()
        return obj

    def list(self, request, org):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(RetentionPolicySerializer(page, many=True).data)

    def create(self, request, org):
        ser = RetentionPolicySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get("mode") == "enforce":
            raise Conflict("Create the policy in dry_run mode and run a dry run before enforcing it.",
                           code="retention_dry_run_required")
        if RetentionPolicy.objects.filter(target=ser.validated_data["target"]).exists():
            raise Conflict("A retention policy for this target already exists.", code="duplicate")
        obj = ser.save(created_by=request.user.pk)
        audit.record("governance.retention.create", target=obj, request=request,
                     metadata={"target": obj.target, "retain_days": obj.retain_days, "action": obj.action,
                               "mode": obj.mode})
        return Response(RetentionPolicySerializer(obj).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, org, pk=None):
        return Response(RetentionPolicySerializer(self._get(pk)).data)

    def partial_update(self, request, org, pk=None):
        obj = self._get(pk)
        before = RetentionPolicySerializer(obj).data
        ser = RetentionPolicySerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        window_changed = any(k in data and data[k] != getattr(obj, k) for k in ("retain_days", "action"))
        if data.get("mode", obj.mode) == "enforce" and (obj.last_dry_run_at is None or window_changed):
            raise Conflict("Run a dry run with these settings before enforcing the policy.",
                           code="retention_dry_run_required")
        obj = ser.save()
        if window_changed:  # a new window or action invalidates the previous dry run
            obj.last_dry_run_at, obj.last_dry_run = None, None
            obj.save(update_fields=["last_dry_run_at", "last_dry_run", "updated_at"])
        after = RetentionPolicySerializer(obj).data
        changes = {k: [before.get(k), after.get(k)] for k in data if before.get(k) != after.get(k)}
        audit.record("governance.retention.update", target=obj, changes=changes or None, request=request,
                     metadata={"target": obj.target})
        return Response(after)

    def destroy(self, request, org, pk=None):
        obj = self._get(pk)
        audit.record("governance.retention.delete", target=obj, request=request,
                     metadata={"target": obj.target, "retain_days": obj.retain_days})
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def dry_run(self, request, org, pk=None):
        obj = self._get(pk)
        try:
            result = retention.dry_run(obj)
        except retention.RetentionError as exc:
            raise Conflict(str(exc), code=exc.code) from exc
        audit.record("governance.retention.dry_run", target=obj, request=request,
                     metadata={"target": obj.target, **result})
        return Response({"policy": RetentionPolicySerializer(obj).data, "dry_run": result})

    def apply(self, request, org, pk=None):
        obj = self._get(pk)
        if request.data.get("confirm") is not True:
            raise ValidationError({"confirm": ["Send confirm: true to delete or archive the matched data."]})
        try:
            result = retention.apply(obj, trigger="manual", request=request)
        except retention.RetentionError as exc:
            raise Conflict(str(exc), code=exc.code) from exc
        return Response({"policy": RetentionPolicySerializer(obj).data, "result": result})


class RetentionTargetsView(TenantAPIView):
    """GET /retention-targets -> {items: [{key, label, description, actions}], never: [append-only tables]}"""

    permission_classes = PERMS
    required_permissions = "governance.retention.edit"

    def get(self, request, org):
        return Response({"items": [retention.target_payload(t) for t in retention.targets()],
                         "never": sorted(retention.NEVER_TARGETS)})
