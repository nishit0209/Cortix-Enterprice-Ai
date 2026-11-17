from django.apps import AppConfig


class SqlguardConfig(AppConfig):
    name = "apps.sqlguard"
    label = "sqlguard"
    verbose_name = "SQL guard"
    default_auto_field = "django.db.models.BigAutoField"
