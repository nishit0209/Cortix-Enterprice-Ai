from django.urls import path

from . import views as v

urlpatterns: list = [
    path("customers", v.ProfileListView.as_view(), name="customer-list"),
    path("customers/settings", v.SettingsView.as_view(), name="customer-settings"),
    path("customers/sources", v.SourceListView.as_view(), name="customer-source-list"),
    path("customers/sources/<uuid:pk>", v.SourceDetailView.as_view(), name="customer-source-detail"),
    path("customers/resolve", v.ResolveView.as_view(), name="customer-resolve"),
    path("customers/resolution-runs", v.ResolutionRunListView.as_view(), name="customer-resolution-run-list"),
    path("customers/resolution-runs/<uuid:pk>", v.ResolutionRunDetailView.as_view(),
         name="customer-resolution-run-detail"),
    path("customers/matches", v.MatchListView.as_view(), name="customer-match-list"),
    path("customers/matches/<uuid:pk>", v.MatchDetailView.as_view(), name="customer-match-detail"),
    path("customers/matches/<uuid:pk>/decision", v.MatchDecisionView.as_view(), name="customer-match-decision"),
    path("customers/segments", v.SegmentListView.as_view(), name="customer-segment-list"),
    path("customers/segments/<uuid:pk>", v.SegmentDetailView.as_view(), name="customer-segment-detail"),
    path("customers/segments/<uuid:pk>/members", v.SegmentMembersView.as_view(), name="customer-segment-members"),
    path("customers/segments/<uuid:pk>/refresh", v.SegmentRefreshView.as_view(), name="customer-segment-refresh"),
    path("customers/scoring-runs", v.ScoringRunView.as_view(), name="customer-scoring-runs"),
    path("customers/features/publish", v.FeaturesPublishView.as_view(), name="customer-features-publish"),
    path("customers/<uuid:pk>", v.ProfileDetailView.as_view(), name="customer-detail"),
    path("customers/<uuid:pk>/timeline", v.ProfileTimelineView.as_view(), name="customer-timeline"),
    path("customers/<uuid:pk>/scores", v.ProfileScoresView.as_view(), name="customer-scores"),
]
