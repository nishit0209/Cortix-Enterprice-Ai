from django.apps import AppConfig

LOG_TRIM_EVERY_S = 3600


class PlatformAdminConfig(AppConfig):
    name = "apps.platform_admin"
    label = "platform_admin"
    verbose_name = "Platform admin"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core import scheduler

        from . import logs

        logs.install()  # WARNING+ records -> the bounded AppLogRecord ring (no-op under tests / the eager runner)
        scheduler.register_periodic("platform.trim_logs", LOG_TRIM_EVERY_S, logs.trim_job)
