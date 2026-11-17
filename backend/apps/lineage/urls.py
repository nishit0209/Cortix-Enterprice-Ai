from django.urls import path

from .views import LineageView

urlpatterns: list = [
    path("lineage", LineageView.as_view(), name="lineage-graph"),
]
