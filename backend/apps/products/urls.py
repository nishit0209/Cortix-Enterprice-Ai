from django.urls import path

from . import views as v

urlpatterns: list = [
    path("products", v.MasterListView.as_view(), name="product-list"),
    path("products/settings", v.SettingsView.as_view(), name="product-settings"),
    path("products/sources", v.SourceListView.as_view(), name="product-source-list"),
    path("products/sources/<uuid:pk>", v.SourceDetailView.as_view(), name="product-source-detail"),
    path("products/normalize", v.NormalizeView.as_view(), name="product-normalize"),
    path("products/enrich", v.EnrichView.as_view(), name="product-enrich"),
    path("products/runs", v.RunListView.as_view(), name="product-run-list"),
    path("products/runs/<uuid:pk>", v.RunDetailView.as_view(), name="product-run-detail"),
    path("products/matches", v.MatchListView.as_view(), name="product-match-list"),
    path("products/matches/<uuid:pk>", v.MatchDetailView.as_view(), name="product-match-detail"),
    path("products/matches/<uuid:pk>/decision", v.MatchDecisionView.as_view(), name="product-match-decision"),
    path("products/corrections", v.CorrectionListView.as_view(), name="product-correction-list"),
    path("products/corrections/<uuid:pk>/decision", v.CorrectionDecisionView.as_view(),
         name="product-correction-decision"),
    path("products/search", v.SearchView.as_view(), name="product-search"),
    path("products/<uuid:pk>", v.MasterDetailView.as_view(), name="product-detail"),
    path("products/<uuid:pk>/attributes", v.MasterAttributesView.as_view(), name="product-attributes"),
]
