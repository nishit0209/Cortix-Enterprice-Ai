from django.apps import AppConfig

DISPATCH_EVERY_S = 15


class AutomationConfig(AppConfig):
    name = "apps.automation"
    label = "automation"
    verbose_name = "Automation"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core import scheduler
        from apps.core.registry import register_webhook_receiver

        from . import builtin_steps, services

        builtin_steps.register()
        register_webhook_receiver(services.receive_webhook)
        scheduler.register_periodic("automation.dispatch", DISPATCH_EVERY_S, services.dispatch_due)
