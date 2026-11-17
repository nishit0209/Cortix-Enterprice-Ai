"""Tenant API-key management: GET/POST /orgs/{org}/api-keys, DELETE /orgs/{org}/api-keys/{id}.

Routed from apps/orgs/urls.py (the accounts app is not mounted under /orgs/<org>/).
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import mixins, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core import audit
from apps.core.permissions import DenyApiKey, HasOrgPermission
from apps.core.tenancy.views import TenantGenericViewSet

from . import services
from .models import ApiKey


class ApiKeyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, trim_whitespace=True)
    scopes = serializers.ListField(child=serializers.CharField(max_length=60), allow_empty=False, max_length=60)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_scopes(self, scopes: list[str]) -> list[str]:
        tenant = self.context["request"].tenant
        unknown = sorted(set(scopes) - services.API_KEY_ALLOWED_SCOPES)
        if unknown:
            raise serializers.ValidationError(f"Scopes not allowed for API keys: {', '.join(unknown)}.")
        beyond = sorted(set(scopes) - set(tenant.permissions))
        if beyond:
            raise serializers.ValidationError(f"You cannot grant scopes you do not hold: {', '.join(beyond)}.")
        return sorted(set(scopes))

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Expiry must be in the future.")
        return value


def api_key_payload(key: ApiKey, creator=None) -> dict:
    creator = creator or key.created_by
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "scopes": list(key.scopes),
        "status": key.status,
        "created_at": key.created_at,
        "created_by": {"id": str(creator.pk), "email": creator.email, "full_name": creator.full_name},
        "expires_at": key.expires_at,
        "revoked_at": key.revoked_at,
        "last_used_at": key.last_used_at,
    }


class ApiKeyViewSet(mixins.ListModelMixin, TenantGenericViewSet):
    permission_classes = [IsAuthenticated, DenyApiKey, HasOrgPermission]
    required_permissions = "api_keys.manage"
    lookup_value_regex = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

    def get_queryset(self):
        return ApiKey.objects.select_related("created_by").order_by("-created_at")

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([api_key_payload(k) for k in page])

    def create(self, request, *args, **kwargs):
        ser = ApiKeyCreateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        key, token = services.create_api_key(
            name=ser.validated_data["name"],
            scopes=ser.validated_data["scopes"],
            created_by=request.user,
            expires_at=ser.validated_data.get("expires_at"),
        )
        audit.record("api_key.create", target=key,
                     metadata={"name": key.name, "prefix": key.prefix, "scopes": list(key.scopes)}, request=request)
        body = api_key_payload(key, request.user)
        body["secret"] = token  # shown ONCE; only the HMAC is stored
        return Response(body, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        key = self.get_object()
        if key.revoked_at is None:
            key.revoked_at = timezone.now()
            key.save(update_fields=["revoked_at", "updated_at"])
            audit.record("api_key.revoke", target=key, metadata={"name": key.name, "prefix": key.prefix},
                         request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)
