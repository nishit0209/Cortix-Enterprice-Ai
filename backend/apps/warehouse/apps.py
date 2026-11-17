from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    name = "apps.warehouse"
    label = "warehouse"
    verbose_name = "Warehouse"
    default_auto_field = "django.db.models.BigAutoField"
