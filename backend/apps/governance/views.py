"""Governance REST endpoints (API_SPECIFICATION.md Phase 5), all `governance.policy.edit`, never via API keys.

    GET, POST, DELETE            /orgs/{org}/datasets/{id}/acl[/{acl_id}]
    GET, POST, PATCH, DELETE     /orgs/{org}/datasets/{id}/column-policies[/{policy_id}]
    GET, POST, PATCH, DELETE     /orgs/{org}/datasets/{id}/row-policies[/{policy_id}]
    POST                         /orgs/{org}/datasets/{id}/policy-preview
    GET, PUT                     /orgs/{org}/users/{user_id}/attributes
    GET                          /orgs/{org}/access-history   (`audit.view`)
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
from apps.core.registry import asset_exists
from apps.core.tenancy.views import TenantAPIView, TenantGenericViewSet
from apps.orgs.models import Membership

from . import services
from .models import AccessHistory, ColumnPolicy, DatasetAcl, RowPolicy, UserAttribute
from .serializers import (
    ColumnPolicySerializer,
    DatasetAclSerializer,
    PolicyPreviewSerializer,
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
    """Compute the effective policy AS a chosen member (no data is read).

    Body: {user_id, tables?, columns_by_table?, privilege?}. Without tables/columns the dataset schema
    provider registered by the datasets app is used. A user that fails the ACL gets `access: false`."""

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
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"tables": [str(exc)]}) from exc
        return Response({"access": True, "policy": services.policy_to_dict(policy)})


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
