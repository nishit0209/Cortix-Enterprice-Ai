"""DRF integration: resolve the org from the URL, verify membership, enter tenant context.

All tenant-scoped API views MUST use `TenantScopedMixin` (or the ready-made `TenantAPIView`,
`TenantGenericViewSet`, `TenantModelViewSet`). URL kwarg `org` is the org slug.
"""

from __future__ import annotations

from contextlib import ExitStack
from uuid import UUID

from django.db import connection, transaction
from rest_framework import generics, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from .context import TenantInfo, current_actor, current_org, current_tenant, set_db_tenant


def resolve_membership(user_id: UUID | str, org_slug: str) -> TenantInfo | None:
    """Pre-tenant lookup via SECURITY DEFINER function (defined in orgs migrations)."""
    with connection.cursor() as cur:
        cur.execute("SELECT org_id, role_key, permissions FROM nexora_membership_check(%s, %s)", [str(user_id), org_slug])
        row = cur.fetchone()
    if not row:
        return None
    return TenantInfo(org_id=row[0], slug=org_slug, role_key=row[1], permissions=frozenset(row[2] or ()))


class TenantScopedMixin:
    """Resolves `request.tenant` and wraps the whole handler in a tenant transaction."""

    tenant_url_kwarg = "org"

    def dispatch(self, request, *args, **kwargs):
        self._tenant_stack = ExitStack()
        try:
            return super().dispatch(request, *args, **kwargs)
        finally:
            self._tenant_stack.close()

    def resolve_tenant(self, request) -> TenantInfo:
        slug = self.kwargs.get(self.tenant_url_kwarg)
        if not slug:
            raise NotFound()
        pre = getattr(request, "_nexora_api_key_tenant", None)  # set by API-key auth (accounts)
        if pre is not None:
            if pre.slug != slug:
                raise NotFound()
            return pre
        user = request.user
        if not user or not user.is_authenticated:
            raise NotFound()
        info = resolve_membership(user.pk, slug)
        if info is None:
            raise NotFound()  # 404, not 403: never reveal other orgs
        return info

    def initial(self, request, *args, **kwargs):
        self.perform_authentication(request)
        info = self.resolve_tenant(request)
        request.tenant = info
        stack = self._tenant_stack
        stack.enter_context(transaction.atomic())
        set_db_tenant(info.org_id)
        t1 = current_org.set(info.org_id)
        t2 = current_tenant.set(info)
        api_key_id = getattr(request, "_nexora_api_key_id", None)
        actor = ("api_key", api_key_id) if api_key_id else ("user", getattr(request.user, "pk", None))
        t3 = current_actor.set(actor)
        stack.callback(current_actor.reset, t3)
        stack.callback(current_org.reset, t1)
        stack.callback(current_tenant.reset, t2)
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        if getattr(response, "status_code", 200) >= 400 and connection.in_atomic_block:
            transaction.set_rollback(True)
        return super().finalize_response(request, response, *args, **kwargs)


class TenantAPIView(TenantScopedMixin, APIView):
    pass


class TenantGenericAPIView(TenantScopedMixin, generics.GenericAPIView):
    pass


class TenantGenericViewSet(TenantScopedMixin, viewsets.GenericViewSet):
    pass


class TenantModelViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    pass


class TenantReadOnlyModelViewSet(TenantScopedMixin, viewsets.ReadOnlyModelViewSet):
    pass
