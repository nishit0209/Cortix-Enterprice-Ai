from rest_framework.routers import SimpleRouter

from .views import PipelineRunViewSet, PipelineViewSet

router = SimpleRouter(trailing_slash=False)
router.register("pipelines", PipelineViewSet, basename="pipeline")
router.register("pipeline-runs", PipelineRunViewSet, basename="pipeline-run")

urlpatterns: list = [*router.urls]
