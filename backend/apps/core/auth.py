from rest_framework.authentication import SessionAuthentication


class CsrfEnforcedSessionAuthentication(SessionAuthentication):
    """Session cookie auth with CSRF enforced on unsafe methods (DRF default behaviour, made explicit)."""

    def authenticate_header(self, request):
        return 'Session realm="nexora"'  # makes DRF return 401 (not 403) for unauthenticated requests
