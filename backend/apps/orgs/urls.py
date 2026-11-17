"""Tenant routes mounted under /api/v1/orgs/<org>/ (GET/PATCH /orgs/<org> itself lives in urls_public)."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from apps.accounts.api_keys import ApiKeyViewSet

from .views import (
    ActivityView,
    AIConsentGrantView,
    AIConsentRevokeView,
    AIConsentTextView,
    AIPolicyView,
    InvitationViewSet,
    MemberViewSet,
    RoleListView,
)

router = SimpleRouter(trailing_slash=False)
router.register("members", MemberViewSet, basename="member")
router.register("invitations", InvitationViewSet, basename="invitation")
router.register("api-keys", ApiKeyViewSet, basename="api-key")

urlpatterns = [
    path("roles", RoleListView.as_view(), name="role-list"),
    path("ai-policy", AIPolicyView.as_view(), name="ai-policy"),
    path("ai-policy/consent-text", AIConsentTextView.as_view(), name="ai-policy-consent-text"),
    path("ai-policy/consents", AIConsentGrantView.as_view(), name="ai-policy-consents"),
    path("ai-policy/revoke", AIConsentRevokeView.as_view(), name="ai-policy-revoke"),
    path("activity", ActivityView.as_view(), name="org-activity"),
    *router.urls,
]
