from django.urls import path

from .views import AIStatusView, AIUsageView

urlpatterns = [
    path("ai/status", AIStatusView.as_view(), name="ai-status"),
    path("ai/usage", AIUsageView.as_view(), name="ai-usage"),
]
