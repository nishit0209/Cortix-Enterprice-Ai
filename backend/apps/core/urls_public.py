"""Public (non-tenant) core routes, mounted under /api/v1/ by nexora.urls once this module exists."""

from django.urls import path

from .files import download

urlpatterns = [
    path("files/<str:token>", download, name="storage-download"),
]
