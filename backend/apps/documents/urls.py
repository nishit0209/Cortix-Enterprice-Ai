from rest_framework.routers import SimpleRouter

from .views import DocumentCollectionViewSet, DocumentTableViewSet, DocumentViewSet, ExtractionViewSet

router = SimpleRouter(trailing_slash=False)
router.register("documents", DocumentViewSet, basename="document")
router.register("document-collections", DocumentCollectionViewSet, basename="document-collection")
router.register("document-extractions", ExtractionViewSet, basename="document-extraction")
router.register("document-tables", DocumentTableViewSet, basename="document-table")

urlpatterns: list = [*router.urls]
