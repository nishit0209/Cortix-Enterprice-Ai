"""Automation step kinds of the pipelines app (CONTRACT W5 "Automation step registry").

`pipelines.run` {pipeline_id}: starts a run of the pipeline's current version as the workflow actor (who must hold
`pipelines.run`; the DAG's dataset access is re-validated as that actor, exactly as for a manual run). Output:
`{run_id, job_id, number, status, pipeline_id}`. The run finishes asynchronously; its outcome is the event
`pipeline.run_succeeded` / `pipeline.run_failed`.
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import ValidationError

from apps.datasets.automation import register_steps, require_step_permission

RUN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pipeline_id"],
    "properties": {"pipeline_id": {"type": "string", "format": "uuid", "title": "Pipeline"}},
}


def run_pipeline_step(ctx, config: dict) -> dict[str, Any]:
    from . import services

    actor = require_step_permission(ctx, "pipelines.run")
    pipeline_id = (config or {}).get("pipeline_id")
    if not pipeline_id:
        raise ValidationError({"pipeline_id": ["A pipeline is required."]})
    pipeline = services.get_pipeline(pipeline_id)
    run, job = services.trigger_run(pipeline, user_id=actor, trigger="automation")
    return {"run_id": str(run.id), "job_id": str(job.id), "number": run.number, "status": run.status,
            "pipeline_id": str(pipeline.id)}


def register() -> list[str]:
    return register_steps([("pipelines.run", run_pipeline_step, RUN_SCHEMA, "low")])
