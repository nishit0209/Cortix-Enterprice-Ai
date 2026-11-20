from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    AccessHistoryView,
    ColumnPolicyViewSet,
    DatasetAclViewSet,
    PolicyPreviewView,
    RetentionPolicyViewSet,
    RetentionTargetsView,
    RowPolicyViewSet,
    UserAttributesView,
)

DS = r"datasets/(?P<dataset_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"

router = SimpleRouter(trailing_slash=False)
router.register(f"{DS}/acl", DatasetAclViewSet, basename="dataset-acl")
router.register(f"{DS}/column-policies", ColumnPolicyViewSet, basename="column-policy")
router.register(f"{DS}/row-policies", RowPolicyViewSet, basename="row-policy")

urlpatterns = [
    path("datasets/<uuid:dataset_id>/policy-preview", PolicyPreviewView.as_view(), name="policy-preview"),
    path("users/<uuid:user_id>/attributes", UserAttributesView.as_view(), name="user-attributes"),
    path("access-history", AccessHistoryView.as_view(), name="access-history"),
    path("retention-targets", RetentionTargetsView.as_view(), name="retention-targets"),
    path("retention-policies", RetentionPolicyViewSet.as_view({"get": "list", "post": "create"}),
         name="retention-policy-list"),
    path("retention-policies/<uuid:pk>",
         RetentionPolicyViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
         name="retention-policy-detail"),
    path("retention-policies/<uuid:pk>/dry-run", RetentionPolicyViewSet.as_view({"post": "dry_run"}),
         name="retention-policy-dry-run"),
    path("retention-policies/<uuid:pk>/apply", RetentionPolicyViewSet.as_view({"post": "apply"}),
         name="retention-policy-apply"),
    *router.urls,
]
