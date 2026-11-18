"""Connector jobs (queue `ingest` via the `apps.connectors.*` route). Outbound DB connections happen only here."""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.connectors.tasks.test_data_source", long_running=True, acks_late=True)
def test_data_source(*, org_id: str, source_id: str, job_id: str, actor_id: str | None = None) -> dict:
    from .services import run_test

    return run_test(org_id=org_id, source_id=source_id, job_id=job_id)


@shared_task(base=TenantTask, name="apps.connectors.tasks.introspect_data_source", long_running=True, acks_late=True)
def introspect_data_source(*, org_id: str, source_id: str, job_id: str, actor_id: str | None = None) -> dict | None:
    from .services import run_introspect

    return run_introspect(org_id=org_id, source_id=source_id, job_id=job_id)


@shared_task(base=TenantTask, name="apps.connectors.tasks.import_table", long_running=True, acks_late=True)
def import_table(*, org_id: str, source_id: str, import_id: str, job_id: str,
                 actor_id: str | None = None) -> dict | None:
    from .services import run_import

    return run_import(org_id=org_id, source_id=source_id, import_id=import_id, job_id=job_id, actor_id=actor_id)
