from django.apps import AppConfig


class GovernanceConfig(AppConfig):
    name = "apps.governance"
    label = "governance"
    verbose_name = "Governance"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import retention

        retention.register_core_targets()
        retention.register_periodic_job()
