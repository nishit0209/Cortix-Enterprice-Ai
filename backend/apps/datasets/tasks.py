from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.datasets.tasks.ingest_dataset", long_running=True, acks_late=True)
def ingest_dataset(*, org_id: str, dataset_id: str, upload_id: str, job_id: str, options: dict | None = None,
                   actor_id: str | None = None) -> dict | None:
    """queue: ingest. parse -> profile -> warehouse load -> quality (see services.run_ingest)."""
    from .services import run_ingest

    return run_ingest(org_id=org_id, dataset_id=dataset_id, upload_id=upload_id, job_id=job_id, options=options)
