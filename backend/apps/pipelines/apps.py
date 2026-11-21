from django.apps import AppConfig


class PipelinesConfig(AppConfig):
    name = "apps.pipelines"
    label = "pipelines"
    verbose_name = "Pipelines"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.connectors.services import register_usage_checker
        from apps.datasets.services import register_stats_provider
        from apps.datasets.signals import dataset_ingest_finished
        from apps.lineage.services import register_node_resolver

        from . import lineage_nodes, scheduling, services, tasks  # noqa: F401 - tasks registers the Celery tasks
        from .automation_steps import register as register_automation_steps
        from .signals import (
            email_owner_on_failure,
            emit_run_failed_event,
            finalize_on_ingest,
            pipeline_run_failed,
        )

        register_stats_provider(services.stats)
        register_usage_checker(services.uses_data_source)
        register_node_resolver("pipeline", lineage_nodes.resolve_pipelines)
        register_node_resolver("pipeline_run", lineage_nodes.resolve_runs)
        pipeline_run_failed.connect(email_owner_on_failure, dispatch_uid="pipelines.email_owner_on_failure")
        pipeline_run_failed.connect(emit_run_failed_event, dispatch_uid="pipelines.automation_run_failed_event")
        register_automation_steps()  # CONTRACT W5: `pipelines.run` (a no-op without apps.automation)
        dataset_ingest_finished.connect(finalize_on_ingest, dispatch_uid="pipelines.finalize_on_ingest")
        # In-process schedule dispatcher (every runner but Celery; Celery uses the beat PeriodicTask instead).
        # Registration only: no DB access at startup. ImportError -> warning (scheduler not installed).
        scheduling.register_local_dispatcher()
