from django.apps import AppConfig


class OrgsConfig(AppConfig):
    name = "apps.orgs"
    label = "orgs"
    verbose_name = "Organizations"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.notifications import register_members_with_permission

        from .services import members_with_permission

        register_members_with_permission(members_with_permission)
