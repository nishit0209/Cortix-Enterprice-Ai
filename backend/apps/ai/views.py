"""AI platform endpoints (mounted under /api/v1/orgs/<org>/)."""

from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasOrgPermission
from apps.core.tenancy.views import TenantAPIView

from . import services


class AIStatusView(TenantAPIView):
    """GET /orgs/{org}/ai/status — policy, tier, configuration, org budget and breaker state."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = "org.view"

    def get(self, request, *args, **kwargs):
        user_id = request.user.pk if getattr(request.user, "is_authenticated", False) else None
        if getattr(request, "_nexora_api_key_id", None):
            user_id = None
        return Response(services.ai_status(user_id=user_id))


class AIUsageView(TenantAPIView):
    """GET /orgs/{org}/ai/usage?days=7 — usage counters (org admins and auditors)."""

    permission_classes = [IsAuthenticated, HasOrgPermission]
    required_permissions = None

    def check_permissions(self, request):
        super().check_permissions(request)
        tenant = getattr(request, "tenant", None)
        if not tenant or not (tenant.has_perm("org.settings.edit") or tenant.has_perm("audit.view")):
            self.permission_denied(request, message="You do not have permission to view AI usage.")

    def get(self, request, *args, **kwargs):
        raw = request.query_params.get("days", "7")
        try:
            days = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"days": ["Must be an integer between 1 and 90."]}) from None
        if not 1 <= days <= 90:
            raise ValidationError({"days": ["Must be an integer between 1 and 90."]})
        return Response(services.usage_summary(days=days))
