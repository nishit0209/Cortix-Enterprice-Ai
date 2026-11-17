"""Permission classes keyed by permission codenames (resolved per org by TenantScopedMixin).

    class DatasetViewSet(TenantModelViewSet):
        permission_classes = [IsAuthenticated, HasOrgPermission]
        required_permissions = {"list": "datasets.view", "create": "datasets.create", "*": "datasets.view"}
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


def tenant_has_perm(request, codename: str) -> bool:
    tenant = getattr(request, "tenant", None)
    return bool(tenant and tenant.has_perm(codename))
