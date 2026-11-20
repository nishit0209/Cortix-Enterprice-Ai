"""Isolation-matrix factories for /pipelines and /pipeline-runs routes (see backend/testing/isolation.py)."""

from __future__ import annotations

import uuid


def _dag() -> dict:
    return {
        "nodes": [
            {"id": "src", "type": "source.dataset", "config": {"dataset_id": str(uuid.uuid4())}},
            {"id": "dst", "type": "destination.dataset", "config": {"name": "Isolation output"}},
        ],
        "edges": [{"from": "src", "to": "dst"}],
    }


def _pipeline(world):
    from apps.pipelines.models import Pipeline, PipelineVersion

    pipeline = world.resources.get("pipeline")
    if pipeline is None:
        with world.tenant():
            pipeline = Pipeline.objects.create(name=f"Isolation pipeline {world.slug}", owner_id=world.owner.pk,
                                               status="active")
            version = PipelineVersion.objects.create(pipeline=pipeline, version=1, dag=_dag(),
                                                     created_by=world.owner.pk)
            pipeline.current_version = version
            pipeline.save(update_fields=["current_version"])
        world.resources["pipeline"] = pipeline
    return pipeline


def pipeline_factory(world) -> dict[str, str]:
    return {"pk": str(_pipeline(world).id)}


def run_factory(world) -> dict[str, str]:
    from apps.pipelines.models import PipelineRun

    run = world.resources.get("pipeline_run")
    if run is None:
        pipeline = _pipeline(world)
        with world.tenant():
            run = PipelineRun.objects.create(pipeline=pipeline, pipeline_version=pipeline.current_version, number=1,
                                             status="failed", trigger="manual", error_code="cast_failed")
        world.resources["pipeline_run"] = run
    return {"pk": str(run.id)}


ISOLATION_FACTORIES = {
    **{name: pipeline_factory for name in ("pipeline-detail", "pipeline-runs", "pipeline-schedule",
                                            "pipeline-preview", "pipeline-versions")},
    **{name: run_factory for name in ("pipeline-run-detail", "pipeline-run-logs", "pipeline-run-retry",
                                      "pipeline-run-cancel")},
}
