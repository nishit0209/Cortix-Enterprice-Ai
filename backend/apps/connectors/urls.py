from rest_framework.routers import SimpleRouter

from .views import DataSourceViewSet

router = SimpleRouter(trailing_slash=False)
router.register("data-sources", DataSourceViewSet, basename="data-source")

urlpatterns: list = [*router.urls]
