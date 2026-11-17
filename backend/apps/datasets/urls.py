from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import DatasetViewSet, DataStatsView, UploadViewSet

router = SimpleRouter(trailing_slash=False)
router.register("uploads", UploadViewSet, basename="upload")
router.register("datasets", DatasetViewSet, basename="dataset")

urlpatterns: list = [
    path("data-stats", DataStatsView.as_view(), name="data-stats"),
    *router.urls,
]
