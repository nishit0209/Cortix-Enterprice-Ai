"""Celery entry points of the guarded query path (queues routed in nexora/celery.py: query_wh / query_ext).

run_query_wh(org_id=, user_id=, dataset_id=, sql=) -> JSON-safe dict
    effective_policy(user, dataset) -> validate -> execute (warehouse reader role). Never raises for guard or
    query errors: returns {"ok": False, "reject_code", "message"} so callers (bi) can audit and repair.
run_query_ext(...) -> {"ok": False, "reject_code": "execution_error"} until the PostgreSQL connector lands.
"""

from __future__ import annotations

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask


def _rejected(code: str, message: str, **extra) -> dict:
    return {"ok": False, "reject_code": code, "message": message, **extra}


def run_guarded_query(*, org_id, user_id, dataset_id, sql: str, privilege: str = "query") -> dict:
    """Synchronous body shared by the task and by in-process callers (inside the org's tenant context)."""
    from uuid import UUID

    from apps.datasets.services import dataset_schema
    from apps.governance.services import effective_policy
    from apps.warehouse.serialization import rows_to_jsonable
    from nexora.contracts import QueryTarget

    from .exceptions import GuardError
    from .services import execute, validate

    tables, columns_by_table = dataset_schema(UUID(str(dataset_id)))
    policy = effective_policy(user_id, dataset_id, tables, columns_by_table, privilege=privilege)
    result = validate(sql, policy)
    if not result.ok:
        return _rejected(result.reject_code or "invalid_sql", result.message, fingerprint=policy.fingerprint)
    try:
        rs = execute(result, policy, QueryTarget(kind="warehouse", org_id=UUID(str(org_id))))
    except GuardError as exc:
        return _rejected(exc.reject_code, str(exc.detail), executed_sql=result.executed_sql,
                         fingerprint=policy.fingerprint)
    return {
        "ok": True,
        "executed_sql": result.executed_sql,
        "tables": list(result.tables),
        "columns": [{"name": c.name, "type": c.type, "masked": c.masked} for c in rs.columns],
        "rows": rows_to_jsonable(rs.rows),
        "row_count": rs.row_count,
        "truncated": rs.truncated,
        "duration_ms": rs.duration_ms,
        "plan_cost": rs.plan_cost,
        "fingerprint": policy.fingerprint,
    }


@shared_task(base=TenantTask, name="apps.sqlguard.tasks.run_query_wh")
def run_query_wh(*, org_id: str, user_id: str, dataset_id: str, sql: str, privilege: str = "query",
                 actor_id: str | None = None) -> dict:
    """queue: query_wh."""
    return run_guarded_query(org_id=org_id, user_id=user_id, dataset_id=dataset_id, sql=sql, privilege=privilege)


@shared_task(base=TenantTask, name="apps.sqlguard.tasks.run_query_ext")
def run_query_ext(*, org_id: str, user_id: str, dataset_id: str, sql: str, data_source_id: str | None = None,
                  actor_id: str | None = None) -> dict:
    """queue: query_ext (external PostgreSQL sources; available once the connector ships)."""
    from .services import EXTERNAL_NOT_AVAILABLE

    return _rejected("execution_error", EXTERNAL_NOT_AVAILABLE)
