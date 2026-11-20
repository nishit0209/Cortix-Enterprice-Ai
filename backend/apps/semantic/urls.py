from django.urls import path

from .model_views import (
    SemanticModelDetailView,
    SemanticModelGenerateView,
    SemanticModelListView,
    SemanticModelMetricDetailView,
    SemanticModelMetricListView,
    SemanticModelPublishView,
)
from .views import MetricDetailView, MetricListView, SemanticModelRefreshView, SemanticModelView

urlpatterns: list = [
    path("datasets/<uuid:dataset_id>/semantic-model", SemanticModelView.as_view(), name="dataset-semantic-model"),
    path(
        "datasets/<uuid:dataset_id>/semantic-model/refresh",
        SemanticModelRefreshView.as_view(),
        name="dataset-semantic-model-refresh",
    ),
    path("datasets/<uuid:dataset_id>/metrics", MetricListView.as_view(), name="dataset-metrics"),
    path("datasets/<uuid:dataset_id>/metrics/<uuid:metric_id>", MetricDetailView.as_view(), name="dataset-metric"),
    # model-centric routes (API_SPECIFICATION Phase 4, dataset "Semantic" tab)
    path("semantic-models", SemanticModelListView.as_view(), name="semantic-model-list"),
    path("semantic-models/<uuid:pk>", SemanticModelDetailView.as_view(), name="semantic-model-detail"),
    path("semantic-models/<uuid:pk>/generate", SemanticModelGenerateView.as_view(), name="semantic-model-generate"),
    path("semantic-models/<uuid:pk>/publish", SemanticModelPublishView.as_view(), name="semantic-model-publish"),
    path("semantic-models/<uuid:pk>/metrics", SemanticModelMetricListView.as_view(), name="semantic-model-metrics"),
    path(
        "semantic-models/<uuid:pk>/metrics/<uuid:metric_id>",
        SemanticModelMetricDetailView.as_view(),
        name="semantic-model-metric",
    ),
]
