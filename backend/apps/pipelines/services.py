"""Public service API of apps.pipelines. Other apps import only from here.

Pipelines:  create_pipeline / update_pipeline / archive_pipeline / get_pipeline / pipeline_payload
DAG:        validate(dag, user_id) -> DagValidation (structure + config + columns/types + dataset access)
Runs:       trigger_run / create_run / retry_run / cancel_run / get_run / run_payload
Schedules:  set_schedule / delete_schedule / schedule_payload
Preview:    preview(dag, user_id, limit=200, node_id=None) -> {columns, rows, steps, ...}   (synchronous, no writes)
            dry_run(dag, user_id, limit, node_id) -> validation (per-node errors) + preview on a 1,000-row sample
Hooks:      stats() (registered with datasets.register_stats_provider), uses_data_source(source_id)
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any
from uuid import UUID

import polars as pl
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.exceptions import Conflict

from . import engine, scheduling
from . import types as T
from .dag import DagIssue, DagValidation, Node, Resolvers, to_validation_error, validate_dag
from .models import Pipeline, PipelineRun, PipelineRunLog, PipelineSchedule, PipelineVersion

PREVIEW_MAX = 200
PREVIEW_SAMPLE_ROWS = 1000        # source rows read by previews / dry runs (API_SPECIFICATION: 1,000-row sample)
PREVIEW_JOIN_ROWS = 100_000
DEFAULT_MAX_RETRIES = 2


# ---------------------------------------------------------------------------------------------------
# resolvers for DAG validation (other apps through their services only)
# ---------------------------------------------------------------------------------------------------
def _dataset_schema(dataset_id: UUID) -> T.Schema | None:
    from apps.datasets import services as datasets

    try:
        dataset = datasets.get_dataset(dataset_id)
    except NotFound:
        return None
    version = datasets.current_version(dataset)
    if version is None or version.status != "ready":
        return None
    return {c["name"]: c["data_type"] for c in datasets.version_columns(version)}


def _make_check_dataset(user_id):
    def check(dataset_id: UUID) -> str | None:
        from apps.datasets import services as datasets

        try:
            dataset = datasets.get_dataset(dataset_id)
        except NotFound:
            return "dataset_not_found"
        if user_id is not None:
            from apps.governance.services import check_dataset_access

            if not check_dataset_access(user_id, dataset.id, "export"):
                return "dataset_access_denied"
        version = datasets.current_version(dataset)
        if version is None or version.status != "ready":
            return "dataset_not_ready"
        return None

    return check


def _check_source(source_id: UUID) -> str | None:
    from apps.connectors import services as connectors

    try:
        source = connectors.get_source(source_id)
    except NotFound:
        return "data_source_not_found"
    if source.status != "active" or not source.verified_readonly:
        return "data_source_not_verified"
    return None


def _connector_schema(source_id: UUID, schema: str | None, table: str) -> T.Schema | None:
    from apps.connectors import services as connectors

    try:
        source = connectors.get_source(source_id)
    except NotFound:
        return None
    catalog = source.catalog or {}
    wanted_schema = schema or (source.config or {}).get("schema") or "public"
    for s in catalog.get("schemas", []):
        if s.get("name") != wanted_schema:
            continue
        for t in s.get("tables", []):
            if t.get("name") == table:
                # connector frames are text-typed (exact values); cast steps turn them into typed columns
                return {c["name"]: T.TEXT for c in t.get("columns", [])}
    return None


def resolvers_for(user_id) -> Resolvers:
    return Resolvers(dataset_schema=_dataset_schema, connector_schema=_connector_schema,
                     check_dataset=_make_check_dataset(user_id), check_source=_check_source)


def validate(dag: Any, user_id=None) -> DagValidation:
    return validate_dag(dag, resolvers_for(user_id))


def _require_valid(dag: Any, user_id) -> DagValidation:
    result = validate(dag, user_id)
    if not result.ok:
        raise to_validation_error(result.issues)
    return result


# ---------------------------------------------------------------------------------------------------
# pipelines
# ---------------------------------------------------------------------------------------------------
def get_pipeline(pipeline_id, *, include_archived: bool = False) -> Pipeline:
    try:
        pid = pipeline_id if isinstance(pipeline_id, UUID) else UUID(str(pipeline_id))
    except ValueError as exc:
        raise NotFound() from exc
    qs = Pipeline.objects.select_related("current_version")
    if not include_archived:
        qs = qs.exclude(status="archived")
    try:
        return qs.get(id=pid)
    except Pipeline.DoesNotExist as exc:
        raise NotFound() from exc


def _new_version(pipeline: Pipeline, dag: dict, user_id) -> PipelineVersion:
    number = (PipelineVersion.objects.filter(pipeline_id=pipeline.id).aggregate(m=Max("version"))["m"] or 0) + 1
    return PipelineVersion.objects.create(pipeline=pipeline, version=number, dag=dag, created_by=user_id)


def _canonical_dag(dag: Any) -> dict:
    from .dag import DagIn

    return DagIn.model_validate(dag).model_dump(by_alias=True, exclude_none=True, mode="json")


def create_pipeline(*, name: str, dag: dict | None = None, description: str = "", max_retries: int | None = None,
                    retry_backoff_s: int | None = None, notify_on_failure: bool = True, user_id=None,
                    request=None) -> Pipeline:
    if Pipeline.objects.filter(name=name).exists():
        raise Conflict("A pipeline with this name already exists.", code="pipeline_name_taken")
    if dag is not None:
        _require_valid(dag, user_id)
    pipeline = Pipeline(name=name, description=description or "", owner_id=user_id,
                        max_retries=DEFAULT_MAX_RETRIES if max_retries is None else max_retries,
                        retry_backoff_s=60 if retry_backoff_s is None else retry_backoff_s,
                        notify_on_failure=notify_on_failure, status="draft")
    try:
        with transaction.atomic():
            pipeline.save()
    except IntegrityError as exc:
        raise Conflict("A pipeline with this name already exists.", code="pipeline_name_taken") from exc
    if dag is not None:
        version = _new_version(pipeline, _canonical_dag(dag), user_id)
        pipeline.current_version = version
        pipeline.status = "active"
        pipeline.save(update_fields=["current_version", "status", "updated_at"])
    audit.record("pipeline.create", target=pipeline, request=request, metadata={
        "name": name, "version": pipeline.current_version.version if pipeline.current_version else None})
    return pipeline


def update_pipeline(pipeline: Pipeline, data: dict[str, Any], *, user_id=None, request=None) -> Pipeline:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if "name" in data and data["name"] != pipeline.name:
        if Pipeline.objects.filter(name=data["name"]).exclude(id=pipeline.id).exists():
            raise Conflict("A pipeline with this name already exists.", code="pipeline_name_taken")
    for field in ("name", "description", "max_retries", "retry_backoff_s", "notify_on_failure"):
        if field in data and getattr(pipeline, field) != data[field]:
            before[field], after[field] = getattr(pipeline, field), data[field]
            setattr(pipeline, field, data[field])
    if "dag" in data and data["dag"] is not None:
        _require_valid(data["dag"], user_id)
        canonical = _canonical_dag(data["dag"])
        current = pipeline.current_version.dag if pipeline.current_version else None
        if canonical != current:
            version = _new_version(pipeline, canonical, user_id)
            before["version"] = pipeline.current_version.version if pipeline.current_version else None
            after["version"] = version.version
            pipeline.current_version = version
            if pipeline.status == "draft":
                pipeline.status = "active"
    if "status" in data and data["status"] != pipeline.status:
        if data["status"] == "active" and pipeline.current_version is None:
            raise ValidationError({"status": ["Add a valid DAG before activating the pipeline."]})
        before["status"], after["status"] = pipeline.status, data["status"]
        pipeline.status = data["status"]
    if not after:
        return pipeline
    try:
        with transaction.atomic():
            pipeline.save()
    except IntegrityError as exc:
        raise Conflict("A pipeline with this name already exists.", code="pipeline_name_taken") from exc
    audit.record("pipeline.update", target=pipeline, request=request, changes={"before": before, "after": after})
    return pipeline


def archive_pipeline(pipeline: Pipeline, *, request=None) -> None:
    if PipelineRun.objects.filter(pipeline_id=pipeline.id, status__in=PipelineRun.ACTIVE).exists():
        raise Conflict("The pipeline has an active run; cancel it first.", code="pipeline_run_active")
    pipeline.status = "archived"
    pipeline.archived_at = timezone.now()
    pipeline.save(update_fields=["status", "archived_at", "updated_at"])
    PipelineSchedule.objects.filter(pipeline_id=pipeline.id).update(enabled=False, next_run_at=None)
    audit.record("pipeline.delete", target=pipeline, request=request, metadata={"name": pipeline.name, "soft": True})


def last_run(pipeline: Pipeline) -> PipelineRun | None:
    return PipelineRun.objects.filter(pipeline_id=pipeline.id).order_by("-created_at").first()


def pipeline_payload(pipeline: Pipeline, *, with_last_run: bool = True) -> dict[str, Any]:
    version = pipeline.current_version
    schedule = PipelineSchedule.objects.filter(pipeline_id=pipeline.id).first()
    lr = last_run(pipeline) if with_last_run else None
    return {
        "id": str(pipeline.id),
        "name": pipeline.name,
        "description": pipeline.description,
        "status": pipeline.status,
        "dag": version.dag if version else None,
        "version": version.version if version else None,
        "schedule": schedule_payload(schedule) if schedule else None,
        "last_run": run_summary(lr) if lr else None,
        "max_retries": pipeline.max_retries,
        "retry_backoff_s": pipeline.retry_backoff_s,
        "notify_on_failure": pipeline.notify_on_failure,
        "output_dataset_id": str(pipeline.output_dataset_id) if pipeline.output_dataset_id else None,
        "owner_id": str(pipeline.owner_id) if pipeline.owner_id else None,
        "created_at": pipeline.created_at.isoformat(),
        "updated_at": pipeline.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------------------------------
def get_run(run_id) -> PipelineRun:
    try:
        rid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except ValueError as exc:
        raise NotFound() from exc
    try:
        return PipelineRun.objects.select_related("pipeline", "pipeline_version").get(id=rid)
    except PipelineRun.DoesNotExist as exc:
        raise NotFound() from exc


def _enqueue(run: PipelineRun) -> None:
    from .tasks import execute_pipeline_run

    kwargs = {"org_id": str(run.org_id), "run_id": str(run.id),
              "actor_id": str(run.triggered_by) if run.triggered_by else None}
    transaction.on_commit(lambda: execute_pipeline_run.apply_async(kwargs=kwargs))


def create_run(pipeline: Pipeline, *, trigger: str, user_id=None, version: PipelineVersion | None = None,
               parent: PipelineRun | None = None, request=None):
    """Create a queued run (+ job) and enqueue it after commit. One active run per pipeline (409)."""
    locked = Pipeline.objects.select_for_update().get(id=pipeline.id)
    version = version or locked.current_version
    if version is None:
        raise Conflict("The pipeline has no valid DAG yet.", code="pipeline_has_no_version")
    if locked.status not in ("active", "paused"):
        raise Conflict("Archived or draft pipelines cannot run.", code="pipeline_not_runnable")
    if PipelineRun.objects.filter(pipeline_id=locked.id, status__in=PipelineRun.ACTIVE).exists():
        raise Conflict("The pipeline already has a queued or running run.", code="pipeline_run_active")
    number = (PipelineRun.objects.filter(pipeline_id=locked.id).aggregate(m=Max("number"))["m"] or 0) + 1
    job = jobs.create_job("pipeline_run", user_id=user_id, message="Queued")
    validation = validate_dag(version.dag)
    run = PipelineRun.objects.create(
        pipeline=locked, pipeline_version=version, number=number, trigger=trigger, status="queued",
        attempt=1, max_attempts=locked.max_retries + 1, parent_run=parent, triggered_by=user_id, job_id=job.id,
        steps=[m.as_dict() for m in engine.initial_metrics(validation)] if validation.ok else [],
    )
    PipelineRunLog.objects.create(run_id=run.id, level="info", attempt=1,
                                  message=f"Run #{number} queued ({trigger})", context=None)
    audit.record("pipeline.run.create", target=locked, request=request, metadata={
        "run_id": str(run.id), "number": number, "trigger": trigger, "version": version.version,
        "job_id": str(job.id), **({"parent_run_id": str(parent.id)} if parent else {})})
    _enqueue(run)
    return run, job


def trigger_run(pipeline: Pipeline, *, user_id=None, request=None, trigger: str = "manual"):
    if user_id is not None and pipeline.current_version is not None:
        result = validate(pipeline.current_version.dag, user_id)  # access + readiness re-checked as the caller
        if not result.ok:
            raise to_validation_error(result.issues)
    return create_run(pipeline, trigger=trigger, user_id=user_id, request=request)


def retry_run(run: PipelineRun, *, user_id=None, request=None):
    if run.status not in ("failed", "cancelled"):
        raise Conflict("Only failed or cancelled runs can be retried.", code="run_not_retryable")
    pipeline = get_pipeline(run.pipeline_id)
    return create_run(pipeline, trigger="retry", user_id=user_id, version=run.pipeline_version, parent=run,
                      request=request)


def cancel_run(run: PipelineRun, *, request=None) -> PipelineRun:
    locked = PipelineRun.objects.select_for_update().get(id=run.id)
    if locked.status not in PipelineRun.ACTIVE:
        raise Conflict("Only queued or running runs can be cancelled.", code="run_not_cancellable")
    now = timezone.now()
    locked.status = "cancelled"
    locked.finished_at = now
    if locked.started_at:
        locked.duration_ms = int((now - locked.started_at).total_seconds() * 1000)
    locked.save(update_fields=["status", "finished_at", "duration_ms", "updated_at"])
    PipelineRunLog.objects.create(run_id=locked.id, level="warning", attempt=locked.attempt,
                                  message="Run cancelled by a user")
    if locked.job_id:
        jobs.update(locked.job_id, status="cancelled", message="Cancelled")
        jobs.publish(locked.job_id, "done", {"status": "cancelled"})
    audit.record("pipeline.run.cancel", target=locked.pipeline, request=request,
                 metadata={"run_id": str(locked.id), "number": locked.number})
    return locked


def run_summary(run: PipelineRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "number": run.number,
        "status": run.status,
        "trigger": run.trigger,
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": run.duration_ms,
        "rows_in": run.rows_in,
        "rows_out": run.rows_out,
        "rows_rejected": run.rows_rejected,
        "error_code": run.error_code or None,
        "error_message": run.error_message or None,
        "quality_summary": run.quality_summary,
        "job_id": str(run.job_id) if run.job_id else None,
        "created_at": run.created_at.isoformat(),
    }


def run_payload(run: PipelineRun, *, include_logs: bool = True, log_limit: int = 500) -> dict[str, Any]:
    body = {
        **run_summary(run),
        "version": run.pipeline_version.version,
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "triggered_by": str(run.triggered_by) if run.triggered_by else None,
        "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
        "error_detail": run.error_detail,
        "output_dataset_id": str(run.output_dataset_id) if run.output_dataset_id else None,
        "output_dataset_version_id": str(run.output_dataset_version_id) if run.output_dataset_version_id else None,
        "ingest_job_id": str(run.ingest_job_id) if run.ingest_job_id else None,
        "steps": [step_payload(st) for st in (run.steps or [])],
        "attempts": run.attempts or [],
    }
    if include_logs:
        body["logs"] = run_logs(run, limit=log_limit)
    return body


def step_payload(step: dict[str, Any]) -> dict[str, Any]:
    """A recorded step metric plus the `step_key` / `step_type` names the UI uses (additive aliases of id/type)."""
    return {**step, "step_key": step.get("id"), "step_type": step.get("type")}


def run_logs(run: PipelineRun, *, limit: int = 500) -> list[dict[str, Any]]:
    rows = PipelineRunLog.objects.filter(run_id=run.id).order_by("ts", "id")[:limit]
    return [{"ts": r.ts.isoformat(), "level": r.level, "step": r.step_key or None, "step_key": r.step_key or None,
             "attempt": r.attempt, "message": r.message, "context": r.context} for r in rows]


def audit_schedule_skip(pipeline: Pipeline, *, reason: str) -> None:
    audit.record("pipeline.schedule.skipped", target=pipeline, metadata={"reason": reason})


# ---------------------------------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------------------------------
def schedule_payload(schedule: PipelineSchedule) -> dict[str, Any]:
    return {
        "cron": schedule.cron, "timezone": schedule.timezone, "enabled": schedule.enabled, "is_active": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_enqueued_at": schedule.last_enqueued_at.isoformat() if schedule.last_enqueued_at else None,
        "last_run_id": str(schedule.last_run_id) if schedule.last_run_id else None,
    }


def set_schedule(pipeline: Pipeline, *, cron: str, tz: str = "UTC", enabled: bool = True, user_id=None,
                 request=None) -> PipelineSchedule:
    cron = scheduling.validate_cron(cron, tz)
    if enabled and pipeline.current_version is None:
        raise ValidationError({"enabled": ["Add a valid DAG before enabling a schedule."]})
    now = timezone.now()
    schedule = PipelineSchedule.objects.select_for_update().filter(pipeline_id=pipeline.id).first()
    before = schedule_payload(schedule) if schedule else None
    if schedule is None:
        schedule = PipelineSchedule(pipeline=pipeline, created_by=user_id)
    schedule.cron, schedule.timezone, schedule.enabled = cron, tz, enabled
    schedule.next_run_at = scheduling.next_fire(cron, tz, now) if enabled else None
    schedule.save()
    audit.record("pipeline.schedule.set", target=pipeline, request=request,
                 changes={"before": before, "after": schedule_payload(schedule)})
    return schedule


def delete_schedule(pipeline: Pipeline, *, request=None) -> None:
    deleted, _ = PipelineSchedule.objects.filter(pipeline_id=pipeline.id).delete()
    if deleted:
        audit.record("pipeline.schedule.delete", target=pipeline, request=request)


# ---------------------------------------------------------------------------------------------------
# preview (synchronous, read-only)
# ---------------------------------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def frame_payload(frame: pl.DataFrame, limit: int) -> dict[str, Any]:
    head = frame.head(limit)
    return {
        "columns": [{"name": n, "type": T.logical_type(d)} for n, d in head.schema.items()],
        "rows": [[_jsonable(v) for v in row] for row in head.iter_rows()],
        "row_count": head.height,
    }


def _preview_load_dataset(user_id):
    def load(dataset_id, limit):
        from apps.datasets import services as datasets

        dataset = datasets.get_dataset(dataset_id)
        if user_id is not None:
            from apps.governance.services import check_dataset_access

            if not check_dataset_access(user_id, dataset.id, "export"):
                raise engine.RunFailure("dataset_access_denied", "No export access to an input dataset.")
        version = datasets.require_ready_version(dataset)
        return datasets.load_version_frame(version, None, limit)

    return load


def _read_connector_preview(kwargs: dict[str, Any]) -> pl.DataFrame:
    """Bounded external read for a preview. With Celery it runs on a worker (egress isolation, result backend);
    with the in-process runners (thread/eager) it runs here, because there is no separate worker to wait for."""
    from django.conf import settings

    if getattr(settings, "TASK_RUNNER", "celery") == "celery" and not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        from .tasks import preview_connector_source

        out = preview_connector_source.apply_async(kwargs=kwargs).get(timeout=60, disable_sync_subtasks=False)
        return pl.DataFrame({c: out["data"][c] for c in out["columns"]}, schema={c: pl.String for c in out["columns"]})
    from apps.connectors import services as connectors

    return connectors.read_frame(kwargs["data_source_id"], kwargs["table"], schema=kwargs["schema"],
                                 columns=kwargs["columns"], limit=kwargs["limit"], org_id=kwargs["org_id"])


def _preview_load_source(user_id):
    load_dataset = _preview_load_dataset(user_id)

    def load(node: Node, limit):
        cfg = node.config
        if node.type == "source.dataset":
            frame = load_dataset(cfg.dataset_id, limit)
            return frame.select(cfg.columns) if cfg.columns else frame
        from apps.core.tenancy import require_org_id

        kwargs = {"org_id": str(require_org_id()), "data_source_id": str(cfg.data_source_id), "table": cfg.table,
                  "schema": cfg.schema_name, "columns": cfg.columns, "limit": limit}
        return _read_connector_preview(kwargs)

    return load


def _check_node_id(validation: DagValidation, node_id: str | None) -> str | None:
    if node_id in (None, ""):
        return None
    if node_id in validation.folded:
        join = validation.folded[node_id]
        raise ValidationError({"node_id": [f"Node {node_id!r} is the right input of join {join!r}; "
                                           f"preview the join instead."]})
    if node_id not in {n.id for n in validation.order}:
        raise ValidationError({"node_id": [f"Unknown node {node_id!r}."]})
    return node_id


def _execute_preview(validation: DagValidation, user_id, limit: int, node_id: str | None) -> dict[str, Any]:
    io = engine.RunIO(load_source=_preview_load_source(user_id), load_dataset=_preview_load_dataset(user_id))
    try:
        result = engine.execute(validation, io, row_limit=max(limit, PREVIEW_SAMPLE_ROWS),
                                side_row_limit=PREVIEW_JOIN_ROWS, stop_after=node_id)
    except engine.RunFailure as failure:
        return {"ok": False, "error": {"code": failure.code, "message": failure.message, "node": failure.node,
                                       "node_id": failure.node},
                "steps": [], "columns": [], "rows": [], "row_count": 0}
    return {
        "ok": True,
        **frame_payload(result.frame, limit),
        "node_id": node_id,
        "source_rows": result.rows_in,
        "rows_rejected": result.rows_rejected,
        "steps": [step_payload(m.as_dict()) for m in result.steps if not m.type.startswith("destination.")],
    }


def preview(dag: Any, *, user_id=None, limit: int = PREVIEW_MAX, node_id: str | None = None) -> dict[str, Any]:
    """Synchronous dry run of a VALID DAG on a sample (400 with per-node errors otherwise). `node_id` returns the
    output of that node instead of the last step. Nothing is written."""
    limit = max(1, min(int(limit), PREVIEW_MAX))
    validation = _require_valid(dag, user_id)
    node_id = _check_node_id(validation, node_id)
    body = _execute_preview(validation, user_id, limit, node_id)
    body["schemas"] = validation.payload()["schemas"]
    return body


def dry_run(dag: Any, *, user_id=None, limit: int = PREVIEW_MAX, node_id: str | None = None) -> dict[str, Any]:
    """POST /pipelines/{id}/validate: full validation + (when valid) a preview on a 1,000-row sample.

    Always 200. `valid` is the static validation verdict; `ok` is false when validation or the sample run failed.
    `errors` lists per-node problems `{node_id, field: "nodes.<id>.<field>", code, message}`, including a step that
    failed on the sample data.
    """
    limit = max(1, min(int(limit), PREVIEW_MAX))
    validation = validate(dag, user_id)
    body: dict[str, Any] = {**validation.payload(), "ok": validation.ok, "columns": [], "rows": [], "row_count": 0,
                            "steps": [], "node_id": None}
    if not validation.ok:
        return body
    node_id = _check_node_id(validation, node_id)
    result = _execute_preview(validation, user_id, limit, node_id)
    if not result["ok"]:
        err = result["error"]
        issue = DagIssue(err.get("node"), "data" if err.get("node") else "dag", err["code"], err["message"])
        body.update(ok=False, errors=[issue.as_dict()], runtime_error=err)
        return body
    body.update({k: v for k, v in result.items() if k != "ok"})
    return body


# ---------------------------------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------------------------------
def stats() -> dict[str, Any]:
    """data-stats contribution (all from the DB): active pipelines and the runs of the last 7 days.

    `runs_7d.total` counts runs created in the window (any status); succeeded/failed count runs that finished in it.
    """
    since = timezone.now() - dt.timedelta(days=7)
    agg = PipelineRun.objects.filter(finished_at__gte=since).aggregate(
        succeeded=Count("id", filter=Q(status="succeeded")), failed=Count("id", filter=Q(status="failed")))
    return {
        "pipelines_active": Pipeline.objects.filter(status="active").count(),
        "pipelines_total": Pipeline.objects.exclude(status="archived").count(),
        "runs_7d": {"succeeded": int(agg["succeeded"] or 0), "failed": int(agg["failed"] or 0),
                    "total": PipelineRun.objects.filter(created_at__gte=since).count()},
        "runs_active": PipelineRun.objects.filter(status__in=PipelineRun.ACTIVE).count(),
    }


def uses_data_source(source_id: UUID) -> list[str]:
    """Names of non-archived pipelines whose current DAG reads from the data source (blocks its deletion)."""
    from .builder import canonical_type

    names = []
    for p in Pipeline.objects.exclude(status="archived").select_related("current_version"):
        dag = p.current_version.dag if p.current_version else None
        for node in (dag or {}).get("nodes", []):
            if canonical_type(str(node.get("type"))) == "source.connector" and str(
                    (node.get("config") or {}).get("data_source_id")) == str(source_id):
                names.append(p.name)
                break
    return names
