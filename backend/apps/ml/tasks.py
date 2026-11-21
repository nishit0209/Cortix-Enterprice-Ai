"""apps.ml background tasks (queue "ml", routed by `apps.ml.*` in nexora.celery).

Every tenant task is a long-running `TenantTask`: the body opens its own short `tenant_step(org)` transactions so
progress events are visible while a model trains. `drift_scan` is a non-tenant ops task (`NexoraTask`) that opens
a tenant context per org itself.
"""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import NexoraTask, TenantTask


@shared_task(base=TenantTask, name="apps.ml.tasks.run_eda_task", long_running=True, acks_late=True)
def run_eda_task(*, org_id: str, project_id: str, job_id: str, user_id: str | None = None, narrate: bool = False,
                 actor_id: str | None = None) -> dict | None:
    from .services import run_eda

    return run_eda(org_id=org_id, project_id=project_id, job_id=job_id, user_id=user_id, narrate_flag=narrate)


@shared_task(base=TenantTask, name="apps.ml.tasks.run_snapshot_task", long_running=True, acks_late=True)
def run_snapshot_task(*, org_id: str, snapshot_id: str, job_id: str, user_id: str | None = None,
                      actor_id: str | None = None) -> dict | None:
    from .services import run_snapshot

    return run_snapshot(org_id=org_id, snapshot_id=snapshot_id, job_id=job_id, user_id=user_id)


@shared_task(base=TenantTask, name="apps.ml.tasks.run_training_task", long_running=True, acks_late=True)
def run_training_task(*, org_id: str, job_id: str, experiment_ids: list[str], snapshot_id: str,
                      user_id: str | None = None, actor_id: str | None = None) -> dict | None:
    from .services import run_training

    return run_training(org_id=org_id, job_id=job_id, experiment_ids=experiment_ids, snapshot_id=snapshot_id,
                        user_id=user_id)


@shared_task(base=TenantTask, name="apps.ml.tasks.run_batch_prediction_task", long_running=True, acks_late=True)
def run_batch_prediction_task(*, org_id: str, model_id: str, version: int, job_id: str, dataset_id: str,
                              output_name: str, output_dataset_id: str | None = None, explain: bool = False,
                              user_id: str | None = None, actor_id: str | None = None,
                              classification: str = "internal", attest: bool = False) -> dict | None:
    from .serving import run_batch_prediction

    return run_batch_prediction(org_id=org_id, model_id=model_id, version=version, job_id=job_id,
                                dataset_id=dataset_id, output_name=output_name, output_dataset_id=output_dataset_id,
                                explain=explain, user_id=user_id, classification=classification, attest=attest)


@shared_task(base=NexoraTask, name="apps.ml.tasks.drift_scan", ignore_result=True)
def drift_scan() -> dict:
    from .serving import run_drift_all

    return run_drift_all()
