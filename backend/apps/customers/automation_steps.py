"""Automation step kinds of Customer 360 (CONTRACT W5 "Automation step registry").

`customers.resolve` {}                                   -> {run_id, job_id, status}      needs customers.manage
`customers.score` {score_type: churn|clv|next_best_action, model_id?}
                                                         -> {run_id, job_id, score_type, model_id, model_version}
                                                                                          needs customers.manage
Both start the same 202 jobs as the API, as the workflow actor (permissions re-checked on every step run).
"""

from __future__ import annotations

from typing import Any

from apps.datasets.automation import register_steps, require_step_permission

RESOLVE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}
SCORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score_type": {"type": "string", "enum": ["churn", "clv", "next_best_action"], "default": "churn",
                       "title": "Score"},
        "model_id": {"type": "string", "format": "uuid", "title": "Model (optional; auto-selected when empty)"},
    },
}


def resolve_step(ctx, config: dict) -> dict[str, Any]:
    from . import services

    actor = require_step_permission(ctx, "customers.manage")
    run, job = services.start_resolution(user_id=actor)
    return {"run_id": str(run.id), "job_id": str(job.id), "status": run.status}


def score_step(ctx, config: dict) -> dict[str, Any]:
    from . import services

    actor = require_step_permission(ctx, "customers.manage")
    run, job = services.start_scoring(dict(config or {}), user_id=actor)
    return {"run_id": str(run.id), "job_id": str(job.id), "score_type": run.score_type,
            "model_id": str(run.model_id) if run.model_id else None, "model_version": run.model_version,
            "selection": run.selection}


def register() -> list[str]:
    return register_steps([
        ("customers.resolve", resolve_step, RESOLVE_SCHEMA, "low"),
        ("customers.score", score_step, SCORE_SCHEMA, "low"),
    ])
