from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    name = "apps.platform_admin"
    label = "platform_admin"
    verbose_name = "Platform admin"
    default_auto_field = "django.db.models.BigAutoField"
