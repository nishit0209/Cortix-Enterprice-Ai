"""API-key authentication (Authorization: Bearer nxk_<prefix>_<secret>).

Contract: on success set `request._nexora_api_key_tenant` (TenantInfo with slug) and
`request._nexora_api_key_id`, return (user_or_service_user, api_key). Return None when no API key header.

* The key is looked up by its public prefix through the SECURITY DEFINER function
  `nexora_api_key_lookup` (no tenant context exists yet), then the secret is verified with a
  constant-time HMAC comparison.
* The authenticated user is the key's creator; the effective permissions are the key's scopes
  intersected with the creator's CURRENT role permissions (removing the creator or downgrading their
  role immediately narrows/kills the key). The key is bound to exactly one org: TenantScopedMixin
  answers 404 for any other org slug.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from apps.core.tenancy.context import TenantInfo, tenant_context

from . import services

_INVALID = "Invalid or expired API key."
LAST_USED_RESOLUTION = timedelta(seconds=60)


class ApiKeyAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword:
            return None
        if len(auth) != 2:
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")
        try:
            raw = auth[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed(_INVALID, code="api_key_invalid") from exc
        if not raw.startswith(services.API_KEY_PREFIX + "_"):
            return None  # some other bearer scheme: not ours
        parsed = services.parse_api_key(raw)
        if parsed is None:
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")
        prefix, secret = parsed
        record = services.lookup_api_key(prefix)
        if record is None:
            services.hash_api_secret(secret)  # equalise timing with the found-key path
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")
        if not services.verify_api_secret(record, secret):
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")
        now = timezone.now()
        if record.revoked_at is not None or (record.expires_at is not None and record.expires_at <= now):
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")

        from .models import ApiKey, User

        user = User.objects.filter(pk=record.created_by, is_active=True).first()
        if user is None:
            raise AuthenticationFailed(_INVALID, code="api_key_invalid")

        if record.last_used_at is None or record.last_used_at < now - LAST_USED_RESOLUTION:
            with tenant_context(record.org_id):
                ApiKey.objects.filter(id=record.id).update(last_used_at=now)

        permissions = frozenset(record.scopes) & record.creator_permissions & services.API_KEY_ALLOWED_SCOPES
        request._nexora_api_key_tenant = TenantInfo(
            org_id=record.org_id, slug=record.org_slug, role_key="api_key", permissions=permissions
        )
        request._nexora_api_key_id = record.id
        return user, record

    def authenticate_header(self, request):
        # Being first in DEFAULT_AUTHENTICATION_CLASSES, this makes DRF answer 401 (not 403).
        return 'Bearer realm="nexora"'
