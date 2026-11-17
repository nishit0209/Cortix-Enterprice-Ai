from django.apps import AppConfig


class QualityConfig(AppConfig):
    name = "apps.quality"
    label = "quality"
    verbose_name = "Data quality"
    default_auto_field = "django.db.models.BigAutoField"
