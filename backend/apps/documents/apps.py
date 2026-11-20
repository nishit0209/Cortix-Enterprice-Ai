from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = "apps.documents"
    label = "documents"
    verbose_name = "Document intelligence"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.registry import register_asset_classifier, register_asset_exists

        from .services import document_classification, document_exists

        register_asset_classifier("document", document_classification)
        register_asset_exists("document", document_exists)
