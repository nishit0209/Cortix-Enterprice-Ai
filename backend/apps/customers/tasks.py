from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.customers.tasks.resolve_customers", long_running=True, acks_late=True)
def resolve_customers(*, org_id: str, run_id: str, actor_id: str | None = None) -> dict | None:
    """Identity resolution (services.run_resolution): sync sources, match, golden profiles, events, segments."""
    from .services import run_resolution

    return run_resolution(org_id=org_id, run_id=run_id)


@shared_task(base=TenantTask, name="apps.customers.tasks.score_customers", long_running=True, acks_late=True)
def score_customers(*, org_id: str, run_id: str, actor_id: str | None = None) -> dict | None:
    """Churn / CLV scoring through the ML registry + next-best-action rules (scoring.run_scoring)."""
    from .scoring import run_scoring

    return run_scoring(org_id=org_id, run_id=run_id)
