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
        from .automation import on_dataset_ingest_finished
        from .services import dataset_classification, dataset_exists, dataset_schema
        from .signals import dataset_ingest_finished

        # CONTRACT W5: every finished ingestion is the automation event `dataset.ingest_finished` (a no-op without
        # matching workflows or without apps.automation).
        dataset_ingest_finished.connect(on_dataset_ingest_finished, dispatch_uid="datasets.automation_ingest_event")

        register_asset_classifier("dataset", dataset_classification)
        register_asset_exists("dataset", dataset_exists)
        register_dataset_schema_provider(dataset_schema)
        register_node_resolver("dataset", lineage_nodes.resolve_datasets)
        register_node_resolver("dataset_version", lineage_nodes.resolve_versions)
        register_node_resolver("upload", lineage_nodes.resolve_uploads)
