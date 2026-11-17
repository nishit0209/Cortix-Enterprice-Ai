"""Permission classes keyed by permission codenames (resolved per org by TenantScopedMixin).

    class DatasetViewSet(TenantModelViewSet):
        permission_classes = [IsAuthenticated, HasOrgPermission]
        required_permissions = {"list": "datasets.view", "create": "datasets.create", "*": "datasets.view"}

Management endpoints that API keys must never reach (members, roles, API keys, AI policy, governance
edits) add `DenyApiKey`.
"""

from rest_framework.permissions import BasePermission


class HasOrgPermission(BasePermission):
    message = "You do not have permission to perform this action."

    def has_permission(self, request, view) -> bool:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return False
        required = getattr(view, "required_permissions", None)
        if not required:
            return True
        if isinstance(required, str):
            codename = required
        else:
            action = getattr(view, "action", None) or request.method.lower()
            codename = required.get(action) or required.get(request.method.lower()) or required.get("*")
        return codename is None or tenant.has_perm(codename)


class DenyApiKey(BasePermission):
    """Refuses requests authenticated with an API key (interactive-user-only endpoints)."""

    message = "API keys cannot access this endpoint."
    code = "api_key_not_allowed"

    def has_permission(self, request, view) -> bool:
        return getattr(request, "_nexora_api_key_id", None) is None


def is_api_key_request(request) -> bool:
    return getattr(request, "_nexora_api_key_id", None) is not None


def tenant_has_perm(request, codename: str) -> bool:
    tenant = getattr(request, "tenant", None)
    return bool(tenant and tenant.has_perm(codename))
