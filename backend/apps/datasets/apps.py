from django.apps import AppConfig


class DatasetsConfig(AppConfig):
    name = "apps.datasets"
    label = "datasets"
    verbose_name = "Datasets"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.registry import (
            register_asset_classifier,
            register_asset_exists,
            register_dataset_schema_provider,
        )
        from apps.lineage.services import register_node_resolver

        from . import lineage_nodes
        from .services import dataset_classification, dataset_exists, dataset_schema

        register_asset_classifier("dataset", dataset_classification)
        register_asset_exists("dataset", dataset_exists)
        register_dataset_schema_provider(dataset_schema)
        register_node_resolver("dataset", lineage_nodes.resolve_datasets)
        register_node_resolver("dataset_version", lineage_nodes.resolve_versions)
        register_node_resolver("upload", lineage_nodes.resolve_uploads)
