from django.apps import AppConfig


class MlConfig(AppConfig):
    name = "apps.ml"
    label = "ml"
    verbose_name = "ML workspace"
    default_auto_field = "django.db.models.BigAutoField"
