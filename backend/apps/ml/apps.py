from django.apps import AppConfig


class MlConfig(AppConfig):
    name = "apps.ml"
    label = "ml"
    verbose_name = "ML workspace"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import serving

        serving.register_periodic_job()  # "ml.drift" hourly: PSI drift (+ label performance) per serving model
        from .steps import register_steps

        register_steps()  # automation step kinds ml.train / ml.batch_predict (no-op without apps.automation)
