"""Logged-in DRF API clients with REAL CSRF enforcement.

    client = api_client_for(user)          # session login + csrftoken cookie + X-CSRFToken header
    client.post(url, {...}, format="json")  # passes CSRF like the SPA does
    raw = api_client_for(user, csrf=False)  # logged in but sends no CSRF header -> unsafe methods get 403

The token is obtained the way the SPA gets it when `GET /api/v1/auth/csrf` exists; otherwise a fresh
CSRF secret is minted locally and set as the `csrftoken` cookie (identical to what Django would set).
"""

from __future__ import annotations

from django.conf import settings
from django.middleware.csrf import CSRF_ALLOWED_CHARS, CSRF_SECRET_LENGTH
from django.urls import Resolver404, resolve
from django.utils.crypto import get_random_string
from rest_framework.test import APIClient

CSRF_ENDPOINT = "/api/v1/auth/csrf"


def route_exists(path: str) -> bool:
    try:
        resolve(path)
    except Resolver404:
        return False
    return True


class CsrfAPIClient(APIClient):
    """APIClient that enforces CSRF checks and automatically attaches the X-CSRFToken header.

    `savepoint_per_request` (default on) runs each request inside its own savepoint when the test already holds
    a transaction (pytest-django `db`). Production requests run in autocommit; without the savepoint, a view
    that calls `transaction.set_rollback(True)` on an error response (TenantScopedMixin.finalize_response does)
    would poison the TEST transaction and the session save would turn a 404 into a 400 (see the QA defect
    "core-finalize-rollback-outer-txn"). Session saving is limited to modified sessions inside that scope.
    """

    def __init__(self, *args, send_csrf: bool = True, savepoint_per_request: bool = True, **kwargs):
        kwargs.setdefault("enforce_csrf_checks", True)
        super().__init__(*args, **kwargs)
        self.send_csrf = send_csrf
        self.savepoint_per_request = savepoint_per_request

    def request(self, **kwargs):
        from django.db import connection, transaction
        from django.test.utils import override_settings

        if not (self.savepoint_per_request and connection.in_atomic_block):
            return super().request(**kwargs)
        with override_settings(SESSION_SAVE_EVERY_REQUEST=False), transaction.atomic():
            return super().request(**kwargs)

    @property
    def csrf_token(self) -> str | None:
        cookie = self.cookies.get(settings.CSRF_COOKIE_NAME)
        return cookie.value if cookie else None

    def ensure_csrf_cookie(self) -> str:
        token = self.csrf_token
        if token:
            return token
        if route_exists(CSRF_ENDPOINT):
            self.get(CSRF_ENDPOINT)
            token = self.csrf_token
        if not token:
            token = get_random_string(CSRF_SECRET_LENGTH, allowed_chars=CSRF_ALLOWED_CHARS)
            self.cookies[settings.CSRF_COOKIE_NAME] = token
        return token

    def generic(self, method, path, data="", content_type="application/octet-stream", secure=False, **extra):
        if self.send_csrf and method.upper() not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            extra.setdefault("HTTP_X_CSRFTOKEN", self.ensure_csrf_cookie())
        return super().generic(method, path, data, content_type, secure=secure, **extra)


def api_client_for(user=None, *, csrf: bool = True, savepoint_per_request: bool = True) -> CsrfAPIClient:
    """A CSRF-enforcing client, logged in as `user` (session auth) when given."""
    client = CsrfAPIClient(send_csrf=csrf, savepoint_per_request=savepoint_per_request)
    if user is not None:
        client.force_login(user)
    client.ensure_csrf_cookie()
    return client


def login_via_api(client: CsrfAPIClient, email: str, password: str):
    """Log in through POST /api/v1/auth/login when the Backend agent's endpoint exists (else force-login)."""
    if route_exists("/api/v1/auth/login"):
        return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")
    from apps.accounts.models import User

    client.force_login(User.objects.get(email=email.lower()))
    return None
