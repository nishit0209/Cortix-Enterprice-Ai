from django.urls import path

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
]
