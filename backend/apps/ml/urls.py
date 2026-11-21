from rest_framework.routers import SimpleRouter

from .views import ExperimentViewSet, ModelViewSet, ProjectViewSet

router = SimpleRouter(trailing_slash=False)
router.register("ml/projects", ProjectViewSet, basename="ml-project")
router.register("ml/experiments", ExperimentViewSet, basename="ml-experiment")
router.register("ml/models", ModelViewSet, basename="ml-model")

urlpatterns: list = [*router.urls]
