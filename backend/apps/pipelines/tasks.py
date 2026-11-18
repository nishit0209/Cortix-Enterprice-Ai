"""Pipeline tasks. Run tasks route to the `ingest` queue (`apps.pipelines.*`); the beat dispatcher is named
`pipelines.dispatch_due` so it stays on the `default` queue and is never stuck behind long ingest work."""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


@shared_task(base=TenantTask, name="apps.pipelines.tasks.execute_pipeline_run", long_running=True, acks_late=True)
def execute_pipeline_run(*, org_id: str, run_id: str, actor_id: str | None = None) -> dict | None:
    from .runner import execute_run

    return execute_run(org_id=org_id, run_id=run_id)


@shared_task(base=TenantTask, name="apps.pipelines.tasks.finalize_pipeline_run", long_running=True, acks_late=True)
def finalize_pipeline_run(*, org_id: str, run_id: str, poll: int = 0, actor_id: str | None = None) -> str:
    from .runner import finalize_run

    return finalize_run(org_id=org_id, run_id=run_id, poll=poll)


@shared_task(base=TenantTask, name="apps.pipelines.tasks.preview_connector_source", long_running=True)
def preview_connector_source(*, org_id: str, data_source_id: str, table: str, schema: str | None = None,
                             columns: list[str] | None = None, limit: int = 200,
                             actor_id: str | None = None) -> dict:
    """Bounded external read for previews, executed on a worker (egress isolation). Returns text columns."""
    from apps.connectors import services as connectors

    frame = connectors.read_frame(data_source_id, table, schema=schema, columns=columns, limit=limit, org_id=org_id)
    return {"columns": frame.columns, "data": {c: frame[c].to_list() for c in frame.columns}}


@shared_task(name="pipelines.dispatch_due", acks_late=False, ignore_result=True)
def dispatch_due() -> dict:
    """Celery beat (every 30 s): fire due schedules per org and reap runs whose worker died."""
    from .scheduling import dispatch_due as _dispatch

    return _dispatch()
