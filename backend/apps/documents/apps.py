from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = "apps.documents"
    label = "documents"
    verbose_name = "Document intelligence"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.ai.reembed import Reembedder, register_reembedder
        from apps.core.registry import register_asset_classifier, register_asset_exists

        from .services import REEMBED_SCAN_SQL, document_classification, document_exists, reembed_pending

        register_asset_classifier("document", document_classification)
        register_asset_exists("document", document_exists)
        # chunks stored while the local embedding model was unavailable get their vectors later
        register_reembedder(Reembedder("documents.chunks", REEMBED_SCAN_SQL, reembed_pending))
