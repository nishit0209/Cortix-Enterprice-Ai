from django.apps import AppConfig


class ProductsConfig(AppConfig):
    name = "apps.products"
    label = "products"
    verbose_name = "Product 360"
    default_auto_field = "django.db.models.BigAutoField"
