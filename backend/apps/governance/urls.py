from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import (
    ColumnPolicyViewSet,
    DatasetAclViewSet,
    PolicyPreviewView,
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
    *router.urls,
]
