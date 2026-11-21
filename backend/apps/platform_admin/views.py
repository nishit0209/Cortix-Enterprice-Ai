"""Platform super-admin API (`/api/v1/platform/...`, API_SPECIFICATION Phase 11).

Only `User.is_platform_admin` (session auth; API keys are never accepted here). Everyone else, anonymous included,
gets 404: the console's existence is not revealed. Everything goes through `services` on the `ops` alias and
returns counts and status only, never tenant data rows.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.auth import CsrfEnforcedSessionAuthentication
from apps.core.pagination import DefaultCursorPagination

from . import services


class IsPlatformAdmin(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user is not None and user.is_authenticated and getattr(user, "is_platform_admin", False)
                and getattr(user, "is_active", True)):
            raise NotFound()
        return True


class PlatformAPIView(APIView):
    authentication_classes = [CsrfEnforcedSessionAuthentication]
    permission_classes = [IsPlatformAdmin]

    def permission_denied(self, request, message=None, code=None):  # unauthenticated -> 404 as well
        raise NotFound()

    def query(self, serializer_class) -> dict:
        ser = serializer_class(data=self.request.query_params)
        ser.is_valid(raise_exception=True)
        return ser.validated_data


# ------------------------------------------------------------------------------------------------ serializers
class OrgListQuery(serializers.Serializer):
    status = serializers.ChoiceField(choices=("active", "suspended", "deleted"), required=False)
    search = serializers.CharField(required=False, max_length=100, allow_blank=True)


class SuspendBody(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)


class ReactivateBody(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")


class JobsQuery(serializers.Serializer):
    queue = serializers.RegexField(r"^[a-z0-9_.-]{1,60}$", required=False)
    status = serializers.ChoiceField(choices=("queued", "running", "succeeded", "failed", "cancelled"),
                                     required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, required=False, default=50)


class AIUsageQuery(serializers.Serializer):
    days = serializers.IntegerField(min_value=1, max_value=90, required=False, default=7)


class ModelQuotaBody(serializers.Serializer):
    rpm = serializers.IntegerField(min_value=1, max_value=1_000_000)
    tpm = serializers.IntegerField(min_value=1, max_value=1_000_000_000)
    rpd = serializers.IntegerField(min_value=1, max_value=100_000_000)
    notes = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    verified = serializers.BooleanField(required=False, default=True)


class LogsQuery(serializers.Serializer):
    level = serializers.ChoiceField(choices=tuple(services.LEVELS), required=False)
    component = serializers.RegexField(r"^[A-Za-z0-9_.-]{1,120}$", required=False)
    before_id = serializers.IntegerField(min_value=1, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=500, required=False, default=100)

    def to_internal_value(self, data):
        if "level" in data:
            data = data.copy()
            data["level"] = str(data["level"]).upper()
        return super().to_internal_value(data)


# ------------------------------------------------------------------------------------------------ orgs
class OrgPagination(DefaultCursorPagination):
    ordering = ("-created_at", "-id")


class OrgListView(PlatformAPIView):
    """GET /platform/orgs?status=&search=&limit=&cursor= (counts and status per org)."""

    def get(self, request):
        q = self.query(OrgListQuery)
        paginator = OrgPagination()
        page = paginator.paginate_queryset(services.orgs_queryset(status=q.get("status"), search=q.get("search")),
                                           request, view=self)
        return paginator.get_paginated_response(services.org_summaries(page))


class OrgDetailView(PlatformAPIView):
    def get(self, request, org_id):
        return Response(services.org_detail(services.get_org(org_id)))


class OrgSuspendView(PlatformAPIView):
    def post(self, request, org_id):
        org = services.get_org(org_id)
        ser = SuspendBody(data=request.data)
        ser.is_valid(raise_exception=True)
        org = services.suspend_org(org, actor=request.user, reason=ser.validated_data["reason"], request=request)
        return Response(services.org_detail(org))


class OrgReactivateView(PlatformAPIView):
    def post(self, request, org_id):
        org = services.get_org(org_id)
        ser = ReactivateBody(data=request.data)
        ser.is_valid(raise_exception=True)
        org = services.reactivate_org(org, actor=request.user, reason=ser.validated_data["reason"], request=request)
        return Response(services.org_detail(org))


# ------------------------------------------------------------------------------------------------ status
class HealthView(PlatformAPIView):
    def get(self, request):
        return Response(services.health())


class JobsView(PlatformAPIView):
    def get(self, request):
        q = self.query(JobsQuery)
        return Response(services.jobs_overview(queue=q.get("queue"), status=q.get("status"), limit=q["limit"]))


class AIUsageView(PlatformAPIView):
    def get(self, request):
        return Response(services.ai_usage(self.query(AIUsageQuery)["days"]))


class ModelQuotaView(PlatformAPIView):
    def put(self, request, model):
        ser = ModelQuotaBody(data=request.data)
        ser.is_valid(raise_exception=True)
        return Response(services.set_model_quota(model, actor=request.user, request=request, **ser.validated_data))


class LogsView(PlatformAPIView):
    def get(self, request):
        q = self.query(LogsQuery)
        return Response(services.recent_logs(level=q.get("level"), component=q.get("component"),
                                             before_id=q.get("before_id"), limit=q["limit"]))
