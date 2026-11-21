"""Automation step kinds of Product 360 (CONTRACT W5 "Automation step registry").

`products.normalize` {} -> {run_id, job_id, status}   needs products.manage (re-checked on every step run)
"""

from __future__ import annotations

from typing import Any

from apps.datasets.automation import register_steps, require_step_permission

NORMALIZE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}


def normalize_step(ctx, config: dict) -> dict[str, Any]:
    from . import services

    actor = require_step_permission(ctx, "products.manage")
    run, job = services.start_run("normalize", user_id=actor)
    return {"run_id": str(run.id), "job_id": str(job.id), "status": run.status}


def register() -> list[str]:
    return register_steps([("products.normalize", normalize_step, NORMALIZE_SCHEMA, "low")])
