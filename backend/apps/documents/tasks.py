from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.documents.tasks.process_document", long_running=True, acks_late=True)
def process_document(*, org_id: str, document_id: str, job_id: str, actor_id: str | None = None) -> dict | None:
    """queue: ml (routed by apps.documents.*). extraction -> OCR -> analysis -> local embeddings -> persist."""
    from .services import run_processing

    return run_processing(org_id=org_id, document_id=document_id, job_id=job_id)
