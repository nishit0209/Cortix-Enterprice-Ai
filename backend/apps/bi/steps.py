"""Automation step `bi.saved_query` (CONTRACT "W5 additions"): runs a saved query through the SQL guard under the
run actor's CURRENT data policy (row/column policies, masks, dataset ACL) and audited like any saved-query run
(`BiQueryAudit`, purpose `saved_query`, actor = the run's user). Only saved queries the actor can see (own or shared, with
read access to the dataset) can run. The output carries the shape and, optionally, the first rows (max 100)."""

from __future__ import annotations

from uuid import UUID

MAX_ROWS = 100

SCHEMA = {
    "type": "object",
    "properties": {
        "saved_query_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "include_rows": {"type": "boolean"},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
    },
    "required": ["saved_query_id"],
    "additionalProperties": False,
}


def saved_query_step(ctx, config: dict) -> dict:
    from django.db.models import Q

    from apps.automation.registry import StepError

    from . import ports, services
    from .models import SavedQuery

    try:
        sq_id = UUID(str(config["saved_query_id"]))
    except ValueError as exc:
        raise StepError("invalid_config", "saved_query_id must be a UUID.", retryable=False) from exc
    sq = SavedQuery.objects.filter(Q(owner_id=ctx.actor_id) | Q(is_shared=True), id=sq_id).first()
    if sq is None or (sq.owner_id != ctx.actor_id
                      and not ports.check_dataset_access(ctx.actor_id, sq.dataset_id, "read")):
        raise StepError("not_found", "The saved query does not exist or is not visible to this run's user.",
                        retryable=False)
    try:
        body = services.run_query(org_id=ctx.org_id, user_id=ctx.actor_id, api_key_id=None,
                                  dataset_id=sq.dataset_id, sql=sq.sql, purpose="saved_query",
                                  question=sq.question or "", chart_hint=sq.chart)
    except services.QueryRejected as exc:
        codes = exc.get_codes()
        raise StepError(codes if isinstance(codes, str) else "sql_rejected", str(exc.detail),
                        retryable=False) from exc
    out = {
        "saved_query_id": str(sq.id), "dataset_id": str(sq.dataset_id), "name": sq.name,
        "query_audit_id": body.get("query_audit_id"), "columns": body.get("columns") or [],
        "row_count": body.get("row_count", 0), "truncated": bool(body.get("truncated")),
    }
    if config.get("include_rows"):
        limit = int(config.get("max_rows") or 20)
        out["rows"] = (body.get("rows") or [])[:limit]
    return out


def register() -> None:
    from apps.automation.registry import register_step

    register_step("bi.saved_query", saved_query_step, schema=SCHEMA, impact="low", permission="dashboards.view",
                  label="Run saved query",
                  description="Runs a saved query through the SQL guard under the run user's data policy.")
