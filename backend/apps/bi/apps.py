from django.apps import AppConfig


class BiConfig(AppConfig):
    name = "apps.bi"
    label = "bi"
    verbose_name = "Conversational BI"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.core.registry import register_policy_sampler

        from . import policy_sample, retention

        retention.register()
        register_policy_sampler(policy_sample.sample_as)
