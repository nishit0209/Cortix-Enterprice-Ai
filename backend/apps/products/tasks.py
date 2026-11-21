from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.products.tasks.normalize_products", long_running=True, acks_late=True)
def normalize_products(*, org_id: str, run_id: str, actor_id: str | None = None) -> dict | None:
    """Sync sources, normalize, match, build masters, attributes, quality scores and search vectors."""
    from .services import run_normalize

    return run_normalize(org_id=org_id, run_id=run_id)


@shared_task(base=TenantTask, name="apps.products.tasks.enrich_products", long_running=True, acks_late=True)
def enrich_products(*, org_id: str, run_id: str, actor_id: str | None = None) -> dict | None:
    """Local rule enrichment: suggestions become proposed corrections (applied only on approval)."""
    from .services import run_enrich

    return run_enrich(org_id=org_id, run_id=run_id)
