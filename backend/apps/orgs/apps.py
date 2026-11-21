from django.apps import AppConfig
from django.db.models.signals import post_migrate


class OrgsConfig(AppConfig):
    name = "apps.orgs"
    label = "orgs"
    verbose_name = "Organizations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.notifications import register_members_with_permission

        from .services import members_with_permission

        register_members_with_permission(members_with_permission)
        post_migrate.connect(_ensure_catalog, sender=self, dispatch_uid="nexora_orgs_permission_catalog")


def _ensure_catalog(sender, using="default", **kwargs) -> None:
    """Keep the global permission catalog complete after `migrate` AND after a test-DB flush (transactional tests
    TRUNCATE every table, wiping the rows the data migrations seeded; Django re-emits post_migrate after a flush)."""
    from django.db import connections

    if "orgs_permission" not in connections[using].introspection.table_names():
        return
    from .services import ensure_permission_catalog

    ensure_permission_catalog()
