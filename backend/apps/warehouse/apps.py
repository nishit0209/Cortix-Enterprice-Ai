from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    name = "apps.warehouse"
    label = "warehouse"
    verbose_name = "Warehouse"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.signals import org_created

        from .receivers import on_org_created

        org_created.connect(on_org_created, dispatch_uid="warehouse_provision_on_org_created")
