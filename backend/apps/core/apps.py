from django.apps import AppConfig
from django.db.models.signals import post_migrate

JOB_EVENT_PURGE_EVERY_S = 6 * 3600


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Core platform (tenancy, audit, jobs)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from celery.signals import beat_init

        from . import scheduler
        from .db import on_post_migrate
        from .ops import purge_job_events

        post_migrate.connect(on_post_migrate, dispatch_uid="nexora_apply_grants")
        scheduler.register_periodic("core.purge_job_events", JOB_EVENT_PURGE_EVERY_S, purge_job_events)
        beat_init.connect(scheduler.install_beat_entries, dispatch_uid="nexora_beat_entries", weak=False)
        # Server processes only (runserver child / uvicorn / daphne / ...); the first tick waits a few seconds,
        # so every app's ready() has registered its jobs by then.
        scheduler.maybe_start()
