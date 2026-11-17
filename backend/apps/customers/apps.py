from django.apps import AppConfig


class CustomersConfig(AppConfig):
    name = "apps.customers"
    label = "customers"
    verbose_name = "Customer 360"
    default_auto_field = "django.db.models.BigAutoField"
