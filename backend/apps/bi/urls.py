from django.urls import path, re_path

from .views import (
    UUID_RE,
    ConversationViewSet,
    DashboardDetailView,
    DashboardListView,
    DashboardWidgetDataView,
    DashboardWidgetDetailView,
    DashboardWidgetListView,
    ExportView,
    InsightsView,
    ManualQueryView,
    MessageFeedbackView,
    QueryHistoryView,
    SavedQueryViewSet,
    UsageView,
)

conversation_list = ConversationViewSet.as_view({"get": "list", "post": "create"})
conversation_detail = ConversationViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
conversation_messages = ConversationViewSet.as_view({"get": "messages", "post": "messages"})
saved_query_list = SavedQueryViewSet.as_view({"get": "list", "post": "create"})
saved_query_detail = SavedQueryViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
saved_query_run = SavedQueryViewSet.as_view({"post": "run"})

urlpatterns = [
    path("bi/conversations", conversation_list, name="bi-conversation-list"),
    re_path(rf"^bi/conversations/(?P<pk>{UUID_RE})$", conversation_detail, name="bi-conversation-detail"),
    re_path(rf"^bi/conversations/(?P<pk>{UUID_RE})/messages$", conversation_messages,
            name="bi-conversation-messages"),
    re_path(rf"^bi/messages/(?P<pk>{UUID_RE})/feedback$", MessageFeedbackView.as_view(), name="bi-message-feedback"),
    path("bi/queries", ManualQueryView.as_view(), name="bi-queries"),
    path("bi/query-history", QueryHistoryView.as_view(), name="bi-query-history"),
    path("saved-queries", saved_query_list, name="bi-saved-query-list"),
    re_path(rf"^saved-queries/(?P<pk>{UUID_RE})$", saved_query_detail, name="bi-saved-query-detail"),
    re_path(rf"^saved-queries/(?P<pk>{UUID_RE})/run$", saved_query_run, name="bi-saved-query-run"),
    path("exports", ExportView.as_view(), name="bi-exports"),
    path("insights", InsightsView.as_view(), name="bi-insights"),
    path("usage", UsageView.as_view(), name="org-usage"),
    # dashboards (CONTRACT "W3 additions")
    path("dashboards", DashboardListView.as_view(), name="bi-dashboard-list"),
    re_path(rf"^dashboards/(?P<pk>{UUID_RE})$", DashboardDetailView.as_view(), name="bi-dashboard-detail"),
    re_path(rf"^dashboards/(?P<pk>{UUID_RE})/widgets$", DashboardWidgetListView.as_view(),
            name="bi-dashboard-widgets"),
    re_path(rf"^dashboards/(?P<pk>{UUID_RE})/widgets/(?P<widget_id>{UUID_RE})$",
            DashboardWidgetDetailView.as_view(), name="bi-dashboard-widget-detail"),
    re_path(rf"^dashboards/(?P<pk>{UUID_RE})/widgets/(?P<widget_id>{UUID_RE})/data$",
            DashboardWidgetDataView.as_view(), name="bi-dashboard-widget-data"),
]
