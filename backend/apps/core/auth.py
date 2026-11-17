from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import PermissionDenied


class CsrfEnforcedSessionAuthentication(SessionAuthentication):
    """Session cookie auth with CSRF enforced on unsafe methods (DRF default behaviour, made explicit)."""

    def authenticate_header(self, request):
        return 'Session realm="nexora"'  # makes DRF return 401 (not 403) for unauthenticated requests


def enforce_csrf(request) -> None:
    """Enforce CSRF on endpoints reachable WITHOUT a session (login, signup, invitation accept), where DRF's
    SessionAuthentication would skip the check. Prevents login-CSRF. The SPA sends X-CSRFToken after
    GET /auth/csrf."""
    try:
        SessionAuthentication().enforce_csrf(request)
    except PermissionDenied as exc:
        raise PermissionDenied(exc.detail, code="csrf_failed") from exc
