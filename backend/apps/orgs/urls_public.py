"""Non-tenant org routes mounted at /api/v1/: create/list orgs, the org detail (no trailing slash),
invitation acceptance and the permission catalog."""

from django.urls import path

from .views import InvitationAcceptView, OrgDetailView, OrgListCreateView, PermissionCatalogView

urlpatterns = [
    path("orgs", OrgListCreateView.as_view(), name="org-list-create"),
    path("orgs/<slug:org>", OrgDetailView.as_view(), name="org-detail"),
    path("invitations/accept", InvitationAcceptView.as_view(), name="invitation-accept"),
    path("permissions", PermissionCatalogView.as_view(), name="permission-catalog"),
]
