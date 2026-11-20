from django.apps import AppConfig


class SemanticConfig(AppConfig):
    name = "apps.semantic"
    label = "semantic"
    verbose_name = "Semantic layer"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.datasets.signals import dataset_version_published

        from .services import on_version_published

        dataset_version_published.connect(on_version_published, dispatch_uid="semantic.refresh_on_publish")
