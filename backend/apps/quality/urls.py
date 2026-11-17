from django.urls import path

from .views import DatasetQualityView, QualityEvaluateView, QualityRuleDetailView, QualityRuleListView

urlpatterns: list = [
    path("datasets/<uuid:dataset_id>/quality", DatasetQualityView.as_view(), name="dataset-quality"),
    path("datasets/<uuid:dataset_id>/quality/evaluate", QualityEvaluateView.as_view(), name="dataset-quality-evaluate"),
    path("datasets/<uuid:dataset_id>/quality-rules", QualityRuleListView.as_view(), name="dataset-quality-rules"),
    path(
        "datasets/<uuid:dataset_id>/quality-rules/<uuid:rule_id>",
        QualityRuleDetailView.as_view(),
        name="dataset-quality-rule",
    ),
]
