from .context import current_org, current_tenant


class TenantContextClearMiddleware:
    """Safety net: guarantees no tenant ContextVar survives a request.

    It never SETS the tenant: DRF authenticates at view dispatch, so tenant resolution happens in
    `apps.core.tenancy.views.TenantScopedMixin.initial()`.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        t1 = current_org.set(None)
        t2 = current_tenant.set(None)
        try:
            return self.get_response(request)
        finally:
            current_tenant.reset(t2)
            current_org.reset(t1)
