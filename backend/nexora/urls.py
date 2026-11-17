"""Root URLconf (Integrator-owned, pre-wired for every app).

Tenant-scoped routes:  /api/v1/orgs/<org>/...   -> each app's `urls.py`
Public/auth routes:    /api/v1/...              -> `apps.accounts.urls_public`, `apps.orgs.urls_public`
Platform routes:       /api/v1/platform/...     -> `apps.platform_admin.urls`
"""

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView

from apps.core.views import healthz, readyz

TENANT_APPS = [
    "apps.orgs",
    "apps.core",
    "apps.governance",
    "apps.ai",
    "apps.connectors",
    "apps.datasets",
    "apps.warehouse",
    "apps.quality",
    "apps.lineage",
    "apps.pipelines",
    "apps.semantic",
    "apps.sqlguard",
    "apps.nl2sql",
    "apps.bi",
    "apps.documents",
    "apps.ml",
    "apps.customers",
    "apps.products",
    "apps.automation",
]

tenant_patterns = [path("", include(f"{app}.urls")) for app in TENANT_APPS]

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("api/v1/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/", include("apps.accounts.urls_public")),
    path("api/v1/", include("apps.orgs.urls_public")),
    path("api/v1/platform/", include("apps.platform_admin.urls")),
    path("api/v1/orgs/<slug:org>/", include(tenant_patterns)),
]
