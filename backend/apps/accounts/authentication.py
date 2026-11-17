"""API-key authentication (Authorization: Bearer nxk_<prefix>_<secret>). Implemented by the Backend agent.

Contract: on success set `request._nexora_api_key_tenant` (TenantInfo with slug) and
`request._nexora_api_key_id`, return (user_or_service_user, api_key). Return None when no API key header.
"""

from rest_framework.authentication import BaseAuthentication


class ApiKeyAuthentication(BaseAuthentication):
    def authenticate(self, request):
        return None
