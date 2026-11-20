from django.apps import AppConfig


class ConnectorsConfig(AppConfig):
    name = "apps.connectors"
    label = "connectors"
    verbose_name = "Connectors"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.registry import register_asset_exists
        from apps.lineage.services import register_node_resolver

        from . import lineage_nodes
        from .services import source_exists

        register_node_resolver("data_source", lineage_nodes.resolve_sources)
        register_asset_exists("data_source", source_exists)
