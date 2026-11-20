"""Policy preview sample (API_SPECIFICATION Phase 5 `POST /datasets/{id}/policy-preview` with `sample: true`).

Runs `SELECT <allowed columns> FROM <table> LIMIT n` through the REAL guard + executor under a GIVEN QueryPolicy
(governance passes the impersonated member's policy intersected with the admin's own, so the admin never sees
more than either of them may see). Audited as a BiQueryAudit row (purpose `policy_preview`, user = the admin) plus
access history. Registered with `apps.core.registry.register_policy_sampler` in BiConfig.ready().
"""

from __future__ import annotations

from uuid import UUID

from nexora.contracts import QueryPolicy

from .results import serialize_result
from .services import AuditInfo, DatasetScope, _semantics, run_guarded

MAX_SAMPLE_ROWS = 50


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sample_sql(policy: QueryPolicy, ordered_columns: dict[str, tuple[str, ...]], limit: int) -> tuple[str, str]:
    """(logical table, SQL) for the first table the policy allows, columns in their dataset order."""
    for table in ordered_columns:
        allowed = policy.allowed_columns.get(table)
        if not allowed:
            continue
        cols = [c for c in ordered_columns[table] if c in allowed] or sorted(allowed)
        limit = max(1, min(int(limit), MAX_SAMPLE_ROWS))
        # identifiers are quoted and the SQL still goes through sqlguard.validate before it runs
        return table, f"SELECT {', '.join(quote_ident(c) for c in cols)} FROM {quote_ident(table)} LIMIT {limit}"  # noqa: S608
    raise LookupError("no table of this dataset is visible under the policy")


def sample_as(policy: QueryPolicy, *, actor_id: UUID, impersonated_user_id: UUID, limit: int = 20) -> dict:
    """Inside the request's tenant context. Never raises for query-level problems: returns `ok: false` + code."""
    semantics = _semantics(policy.dataset_id)
    try:
        table, sql = sample_sql(policy, dict(semantics.columns_by_table), limit)
    except LookupError:
        return {"ok": False, "code": "no_visible_columns", "detail": "The member cannot see any column of this "
                "dataset, so there is nothing to sample.", "query_audit_id": None}
    info = AuditInfo(org_id=policy.org_id, user_id=actor_id, api_key_id=None, dataset_id=policy.dataset_id,
                     dataset_name=semantics.dataset_name, purpose="policy_preview",
                     question=f"policy preview as member {impersonated_user_id}", source="manual")
    outcome = run_guarded(sql, DatasetScope(semantics=semantics, policy=policy), info)
    if not outcome.ok:
        return {"ok": False, "code": outcome.code, "detail": outcome.message, "query_audit_id": str(outcome.audit_id)}
    return {"ok": True, "table": table, "executed_sql": outcome.guard.executed_sql,
            "query_audit_id": str(outcome.audit_id), **serialize_result(outcome.result)}

