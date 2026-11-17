from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "Core platform (tenancy, audit, jobs)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from .db import on_post_migrate

        post_migrate.connect(on_post_migrate, dispatch_uid="nexora_apply_grants")
