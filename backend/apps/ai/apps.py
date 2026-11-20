from django.apps import AppConfig


class AiConfig(AppConfig):
    name = "apps.ai"
    label = "ai"
    verbose_name = "AI (Gemini)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import (
            checks,  # noqa: F401 - registers system checks
            quota_store,
            reembed,
        )

        # Both are skipped until apps.core.scheduler exists (Backend, W3).
        reembed.register_periodic_job()  # "ai.reembed_pending" every 600 s
        quota_store.register_periodic_job()  # "ai.purge_quota_counters" hourly, only without Redis
