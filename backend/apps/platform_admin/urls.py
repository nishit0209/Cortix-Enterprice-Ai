"""Platform super-admin routes, mounted at /api/v1/platform/ (nexora/urls.py). Not tenant routes."""

from django.urls import path

from . import views

urlpatterns = [
    path("orgs", views.OrgListView.as_view(), name="platform-orgs"),
    path("orgs/<uuid:org_id>", views.OrgDetailView.as_view(), name="platform-org-detail"),
    path("orgs/<uuid:org_id>/suspend", views.OrgSuspendView.as_view(), name="platform-org-suspend"),
    path("orgs/<uuid:org_id>/reactivate", views.OrgReactivateView.as_view(), name="platform-org-reactivate"),
    path("health", views.HealthView.as_view(), name="platform-health"),
    path("jobs", views.JobsView.as_view(), name="platform-jobs"),
    path("ai/usage", views.AIUsageView.as_view(), name="platform-ai-usage"),
    path("ai/model-quotas/<str:model>", views.ModelQuotaView.as_view(), name="platform-ai-model-quota"),
    path("logs", views.LogsView.as_view(), name="platform-logs"),
]
