from django.apps import AppConfig


class ProductsConfig(AppConfig):
    name = "apps.products"
    label = "products"
    verbose_name = "Product 360"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from .automation_steps import register

        register()  # CONTRACT W5 automation step kinds (a no-op without apps.automation)
