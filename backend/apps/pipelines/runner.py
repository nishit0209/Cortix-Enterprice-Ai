"""Pipeline run execution (Celery task bodies, queue `ingest`).

    execute_run(org_id, run_id)   claim -> load source -> steps (Polars, per-step metrics) -> write the output CSV
                                  through the standard upload path (create_upload -> create_dataset/add_version)
    finalize_run(org_id, run_id)  after ingestion: quality summary, lineage, succeeded / failed (+ retry)
    fail_or_retry(...)            retryable failures re-queue the SAME run with exponential backoff
                                  (attempt += 1, up to pipeline.max_retries); others fail it, audit + notify.

Every DB write happens in short `tenant_step` transactions; no transaction spans the heavy work.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import shutil
import tempfile
from typing import Any
from uuid import UUID

import polars as pl
from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.utils import timezone

from apps.core import audit, jobs
from apps.core.models import Job
from apps.core.tenancy.tasks import tenant_step

from . import engine
from .dag import Node, validate_dag
from .engine import RunFailure, StepMetric
from .models import Pipeline, PipelineRun, PipelineRunLog

log = logging.getLogger(__name__)

FINALIZE_POLL_S = 5
FINALIZE_MAX_POLLS = 720          # 1 hour of ingestion at most
_INGEST_OPTIONS = {"notify": False}  # the run notifies its owner; no separate "dataset ready" notification


def max_rows() -> int:
    return int(getattr(settings, "PIPELINE_MAX_ROWS", getattr(settings, "DATASET_MAX_ROWS", 50_000_000)))


def _is_eager() -> bool:
    return runner_mode() == "eager"


def runner_mode() -> str:
    """"celery" | "eager" | "thread" (settings.TASK_RUNNER; CONTRACT "Local runtime")."""
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return "eager"
    mode = str(getattr(settings, "TASK_RUNNER", "celery") or "celery")
    return mode if mode in ("celery", "eager", "thread") else "thread"


def enqueue_later(task, kwargs: dict, countdown: int) -> bool:
    """Run `task` after `countdown` seconds (after commit). Returns False when the caller relies on the periodic
    dispatcher instead: the in-process thread runner has no durable delayed queue, so delayed work (retry backoff,
    finalize polling) is picked up by `scheduling.dispatch_due` from the run's own columns."""
    if runner_mode() == "thread":
        return False
    transaction.on_commit(lambda: task.apply_async(kwargs=kwargs, countdown=countdown))
    return True


# ---------------------------------------------------------------------------------------------------
# logging helpers (inside a tenant step)
# ---------------------------------------------------------------------------------------------------
def run_log(run: PipelineRun, message: str, *, level: str = "info", step: str = "", context: dict | None = None):
    PipelineRunLog.objects.create(run_id=run.id, level=level, step_key=step[:40], attempt=run.attempt,
                                  message=message[:1000], context=context)
    if run.job_id:
        jobs.publish(run.job_id, "log", {"level": level, "step": step or None, "message": message,
                                         "attempt": run.attempt, **({"context": context} if context else {})})


def _progress(steps: list[dict]) -> int:
    total = max(1, len(steps))
    done = sum(1 for s in steps if s.get("status") in ("succeeded", "skipped", "failed"))
    return min(90, 5 + int(85 * done / total))


# ---------------------------------------------------------------------------------------------------
# IO for the engine (dataset / connector reads + lineage)
# ---------------------------------------------------------------------------------------------------
class RunContext:
    def __init__(self, org: UUID, run: PipelineRun, actor: UUID | None):
        self.org = org
        self.run_id = run.id
        self.pipeline_id = run.pipeline_id
        self.actor = actor
        self.input_versions: list[str] = []

    def _check_access(self, dataset_id) -> None:
        if self.actor is None:
            return
        from apps.governance.services import check_dataset_access

        if not check_dataset_access(self.actor, dataset_id, "export"):
            raise RunFailure("dataset_access_denied",
                             "The run's user lacks export access to an input dataset.", retryable=False)

    def load_dataset(self, dataset_id, limit: int | None, *, role: str = "join input") -> pl.DataFrame:
        from apps.datasets import services as datasets
        from apps.lineage import services as lineage

        with tenant_step(self.org):
            try:
                dataset = datasets.get_dataset(dataset_id)
                version = datasets.require_ready_version(dataset)
            except Exception as exc:  # noqa: BLE001 - NotFound / DatasetNotReady -> stable codes
                code = "dataset_not_ready" if getattr(exc, "default_code", "") == "dataset_not_ready" \
                    else "dataset_not_found"
                raise RunFailure(code, "An input dataset is missing or has no ready version.",
                                 retryable=False) from None
            self._check_access(dataset.id)
            lineage.record("dataset_version", version.id, "pipeline", self.pipeline_id, run_id=self.run_id,
                           label=role)
            self.input_versions.append(str(version.id))
            return datasets.load_version_frame(version, None, limit)

    def load_source(self, node: Node, limit: int | None) -> pl.DataFrame:
        cfg = node.config
        if node.type == "source.dataset":
            frame = self.load_dataset(cfg.dataset_id, limit, role="input")
            if cfg.columns:
                missing = [c for c in cfg.columns if c not in frame.columns]
                if missing:
                    raise RunFailure("unknown_column", f"Unknown column(s): {', '.join(missing)}.", retryable=False)
                frame = frame.select(cfg.columns)
            return frame
        from apps.connectors import services as connectors
        from apps.lineage import services as lineage

        frame = connectors.read_frame(cfg.data_source_id, cfg.table, schema=cfg.schema_name, columns=cfg.columns,
                                      limit=limit, max_rows=cfg.max_rows or max_rows(), org_id=self.org)
        with tenant_step(self.org):
            lineage.record("data_source", cfg.data_source_id, "pipeline", self.pipeline_id, run_id=self.run_id,
                           label="input", transform={"table": f"{cfg.schema_name or 'public'}.{cfg.table}"})
        return frame

    def on_step(self, metric: StepMetric, metrics: list[StepMetric]) -> None:
        steps = [m.as_dict() for m in metrics]
        with tenant_step(self.org):
            run = PipelineRun.objects.get(id=self.run_id)
            if run.status == "cancelled":
                raise RunFailure("cancelled", "The run was cancelled.", retryable=False)
            PipelineRun.objects.filter(id=self.run_id).update(steps=steps, heartbeat_at=timezone.now())
            if metric.status == "running":
                run_log(run, f"Step {metric.id} ({metric.type}) started", step=metric.id)
            elif metric.status == "succeeded":
                run_log(run, f"Step {metric.id} finished: {metric.rows_out} rows"
                        + (f", {metric.rows_rejected} rejected" if metric.rows_rejected else ""),
                        step=metric.id, context={"rows_in": metric.rows_in, "rows_out": metric.rows_out,
                                                 "rows_rejected": metric.rows_rejected,
                                                 "duration_ms": metric.duration_ms})
            elif metric.status == "failed":
                run_log(run, f"Step {metric.id} failed: {metric.error['message'] if metric.error else ''}",
                        level="error", step=metric.id, context=metric.error)
            if run.job_id:
                jobs.update(run.job_id, status="running", progress=_progress(steps),
                            message=f"{metric.id}: {metric.status}")


# ---------------------------------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------------------------------
def execute_run(*, org_id, run_id) -> dict | None:
    org = UUID(str(org_id))
    with tenant_step(org):
        run = (PipelineRun.objects.select_for_update().select_related("pipeline", "pipeline_version")
               .filter(id=run_id).first())
        if run is None or run.status != "queued":
            return None  # cancelled, or a duplicate delivery of an already running/finished attempt
        now = timezone.now()
        run.status = "running"
        run.started_at = run.started_at or now
        run.heartbeat_at = now
        run.next_attempt_at = None
        run.attempts = [*run.attempts, {"attempt": run.attempt, "started_at": now.isoformat(), "finished_at": None,
                                        "error_code": None}]
        run.save(update_fields=["status", "started_at", "heartbeat_at", "next_attempt_at", "attempts", "updated_at"])
        pipeline: Pipeline = run.pipeline
        dag = run.pipeline_version.dag
        actor = run.triggered_by or pipeline.owner_id
        if run.job_id:
            jobs.update(run.job_id, status="running", progress=5,
                        message=f"Attempt {run.attempt} of {run.max_attempts} started")
        run_log(run, f"Attempt {run.attempt} of {run.max_attempts} started (pipeline v{run.pipeline_version.version})")
    ctx = RunContext(org, run, actor)
    workdir = tempfile.mkdtemp(prefix="nexora_pipeline_")
    try:
        validation = validate_dag(dag)
        if not validation.ok:
            raise RunFailure("dag_invalid", "The pipeline definition is invalid.", retryable=False,
                             detail={"errors": [i.as_dict() for i in validation.issues][:20]})
        io = engine.RunIO(load_source=ctx.load_source, load_dataset=ctx.load_dataset, on_step=ctx.on_step)
        result = engine.execute(validation, io, max_rows=max_rows())
        _write_output(org, run_id, validation.destination, result, ctx, workdir)
        return {"run_id": str(run_id), "status": "running", "rows_out": result.rows_out}
    except RunFailure as failure:
        fail_or_retry(org, run_id, failure)
    except Exception as exc:  # noqa: BLE001 - never leak internals; stable code on the run
        log.exception("pipeline run crashed run=%s", run_id)
        code = exc.get_codes() if hasattr(exc, "get_codes") and isinstance(exc.get_codes(), str) else "run_failed"
        fail_or_retry(org, run_id, RunFailure(code, "The run failed unexpectedly.",
                                              retryable=getattr(exc, "retryable", None)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return None


def _write_output(org: UUID, run_id, dest: Node, result: engine.ExecutionResult, ctx: RunContext,
                  workdir: str) -> None:
    from apps.connectors import services as connectors
    from apps.datasets import services as datasets
    from apps.lineage import services as lineage

    from .tasks import finalize_pipeline_run

    cfg = dest.config
    metrics = result.steps
    dm = next(m for m in metrics if m.id == dest.id)
    dm.status, dm.started_at, dm.rows_in = "running", timezone.now().isoformat(), result.rows_out
    if result.frame.width == 0:
        raise RunFailure("no_columns", "The pipeline output has no columns.", node=dest.id, retryable=False)
    with tenant_step(org):
        run = PipelineRun.objects.select_related("pipeline").get(id=run_id)
        pipeline = run.pipeline
        PipelineRun.objects.filter(id=run_id).update(
            steps=[m.as_dict() for m in metrics], rows_in=result.rows_in, rows_out=result.rows_out,
            rows_rejected=result.rows_rejected, heartbeat_at=timezone.now())
        run_log(run, f"Writing {result.rows_out} rows to the destination", step=dest.id)
    frame = result.frame
    if getattr(cfg, "mode", "new_version") == "append":
        frame = _appended_output(org, run, pipeline, cfg, frame, dest)
    filename = f"pipeline-{_slug(pipeline.name)}-run-{run.number}.csv"
    path = os.path.join(workdir, filename)
    size = connectors.write_frame_csv(frame, path)
    if size > int(settings.MAX_UPLOAD_BYTES):
        raise RunFailure("output_too_large", "The pipeline output is larger than the upload limit.",
                         node=dest.id, retryable=False)
    with tenant_step(org):
        run = PipelineRun.objects.select_related("pipeline").get(id=run_id)
        pipeline = run.pipeline
        actor = ctx.actor
        with open(path, "rb") as fh:
            upload = datasets.create_upload(File(fh, name=filename), user_id=actor)
        target = None
        target_id = cfg.dataset_id or pipeline.output_dataset_id
        if target_id:
            try:
                target = datasets.get_dataset(target_id)
            except Exception:  # noqa: BLE001 - deleted output dataset: create a new one (name-based) / fail
                if cfg.dataset_id:
                    raise RunFailure("dataset_not_found", "The destination dataset no longer exists.",
                                     node=dest.id, retryable=False) from None
                target = None
        if target is not None:
            ingest_job = datasets.add_version(target, upload_id=upload.id, user_id=actor, options=_INGEST_OPTIONS)
            dataset = target
        else:
            dataset, ingest_job = datasets.create_dataset(
                name=cfg.name, upload_id=upload.id, classification=cfg.classification, user_id=actor,
                description=cfg.description or f"Output of pipeline {pipeline.name}", tags=["pipeline"],
                options=_INGEST_OPTIONS,
            )
            Pipeline.objects.filter(id=pipeline.id).update(output_dataset_id=dataset.id)
        dm.status = "running"
        PipelineRun.objects.filter(id=run_id).update(
            output_dataset_id=dataset.id, ingest_job_id=ingest_job.id, steps=[m.as_dict() for m in metrics],
            heartbeat_at=timezone.now())
        lineage.record("pipeline", pipeline.id, "dataset", dataset.id, run_id=run_id, label="writes")
        run_log(run, "Output handed to ingestion (profile, quality, warehouse load)", step=dest.id,
                context={"dataset_id": str(dataset.id), "ingest_job_id": str(ingest_job.id), "upload_id": str(upload.id)})
        if run.job_id:
            jobs.update(run.job_id, status="running", progress=92, message="Ingesting the output")
        kwargs = {"org_id": str(org), "run_id": str(run_id), "poll": 0}
        # registered AFTER create_dataset/add_version's own on_commit, so in eager mode ingestion runs first.
        # Every runner also finalizes from the ingest-finished signal (signals.finalize_on_ingest).
        enqueue_later(finalize_pipeline_run, kwargs, FINALIZE_POLL_S)


def append_frames(previous: pl.DataFrame, new: pl.DataFrame, *, node: str | None = None,
                  limit: int | None = None) -> pl.DataFrame:
    """Destination mode "append": the new version = the previous version's rows followed by the run's rows.

    The run output must have exactly the previous version's columns (any order); its values are cast to the
    previous version's types (strictly: a value that does not fit fails the run instead of becoming NULL).
    Raises RunFailure append_schema_mismatch / append_type_mismatch / output_too_large (never retried)."""
    prev_cols, new_cols = list(previous.columns), list(new.columns)
    missing = [c for c in prev_cols if c not in new_cols]
    extra = [c for c in new_cols if c not in prev_cols]
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing[:10])}")
        if extra:
            parts.append(f"unexpected {', '.join(extra[:10])}")
        raise RunFailure("append_schema_mismatch",
                         "Append needs the same columns as the current version of the output dataset ("
                         + "; ".join(parts) + "). Use mode \"new_version\" to change the schema.",
                         node=node, retryable=False)
    if limit is not None and previous.height + new.height > limit:
        raise RunFailure("output_too_large", f"Appending would exceed the {limit:,}-row limit.", node=node,
                         retryable=False)
    try:
        aligned = new.select([
            pl.col(c) if new.schema[c] == previous.schema[c] or previous.schema[c] == pl.Null
            else pl.col(c).cast(previous.schema[c], strict=True)
            for c in prev_cols
        ])
    except (pl.exceptions.InvalidOperationError, pl.exceptions.ComputeError) as exc:
        raise RunFailure("append_type_mismatch",
                         "The run output has values that do not fit the column types of the current version.",
                         node=node, retryable=False) from exc
    return pl.concat([previous, aligned], how="vertical_relaxed")


def _appended_output(org: UUID, run: PipelineRun, pipeline: Pipeline, cfg, frame: pl.DataFrame, dest: Node):
    """previous rows + `frame` when the destination dataset already has a ready version, else `frame`."""
    from apps.datasets import services as datasets

    target_id = cfg.dataset_id or pipeline.output_dataset_id
    if not target_id:
        return frame  # first run: there is nothing to append to yet
    with tenant_step(org):
        try:
            target = datasets.get_dataset(target_id)
            version = datasets.require_ready_version(target)
        except Exception:  # noqa: BLE001 - deleted / not ready: the run writes its own rows (or fails below)
            if cfg.dataset_id:
                raise RunFailure("dataset_not_ready", "The destination dataset has no ready version to append to.",
                                 node=dest.id, retryable=False) from None
            return frame
        previous = datasets.load_version_frame(version)
        run_log(run, f"Appending {frame.height} rows to version {version.version} ({previous.height} rows)",
                step=dest.id, context={"dataset_id": str(target.id), "previous_version": version.version})
    return append_frames(previous, frame, node=dest.id, limit=max_rows())


def _slug(name: str) -> str:
    from django.utils.text import slugify

    return (slugify(name) or "output")[:60]


def finalize_run(*, org_id, run_id, poll: int = 0) -> str:
    """Complete a run once its output ingestion finished. Returns the outcome (for tests/logging)."""
    from apps.lineage import services as lineage

    from .tasks import finalize_pipeline_run

    org = UUID(str(org_id))
    with tenant_step(org):
        run = PipelineRun.objects.select_for_update().select_related("pipeline").filter(id=run_id).first()
        if run is None or run.status != "running" or run.ingest_job_id is None:
            return "ignored"
        job = Job.objects.filter(id=run.ingest_job_id).first()
        status = job.status if job else "failed"
        if status in ("queued", "running"):
            if poll < FINALIZE_MAX_POLLS and not _is_eager() and not _waited_too_long(run):
                PipelineRun.objects.filter(id=run_id).update(heartbeat_at=timezone.now())
                kwargs = {"org_id": str(org), "run_id": str(run_id), "poll": poll + 1}
                # thread runner: the ingest-finished signal (or the dispatcher's finalize scan) comes back here
                enqueue_later(finalize_pipeline_run, kwargs, FINALIZE_POLL_S)
                return "waiting"
            failure = RunFailure("ingest_timeout", "Ingesting the pipeline output did not finish in time.")
        elif status == "succeeded":
            res: dict[str, Any] = job.result or {}
            summary = {
                "score": res.get("quality_score"), "current": res.get("current", True),
                "blocking_failures": res.get("blocking_failures") or [], "version": res.get("version"),
                "row_count": res.get("row_count"), "column_count": res.get("column_count"),
                "pii_columns": res.get("pii_columns") or [],
                "rules_passed": res.get("rules_passed"), "rules_failed": res.get("rules_failed"),
            }
            version_id = res.get("version_id")
            run.quality_summary = summary
            run.output_dataset_version_id = UUID(version_id) if version_id else None
            if version_id:
                lineage.record("pipeline_run", run.id, "dataset_version", version_id, run_id=run.id, label="produced")
                lineage.record("pipeline", run.pipeline_id, "dataset_version", version_id, run_id=run.id,
                               label=f"run #{run.number}")
            if summary["blocking_failures"] and not summary["current"]:
                run.save(update_fields=["quality_summary", "output_dataset_version_id", "updated_at"])
                failure = RunFailure("quality_blocking_failure",
                                     "Blocking quality rules failed; the new version was not made current.",
                                     retryable=False, detail={"rules": summary["blocking_failures"]})
            else:
                _succeed(run, summary)
                return "succeeded"
        else:
            err = (job.error or {}) if job else {}
            failure = RunFailure(err.get("code") or "ingest_failed",
                                 err.get("message") or "Ingesting the pipeline output failed.")
    fail_or_retry(org, run_id, failure)
    return "failed"


def handoff_at(run: PipelineRun) -> dt.datetime | None:
    """When the output was handed to ingestion (the destination step's start), else the run start."""
    for step in run.steps or []:
        if str(step.get("type", "")).startswith("destination.") and step.get("started_at"):
            try:
                return dt.datetime.fromisoformat(step["started_at"])
            except (TypeError, ValueError):
                break
    return run.started_at


def _waited_too_long(run: PipelineRun) -> bool:
    started = handoff_at(run)
    return started is not None and (timezone.now() - started).total_seconds() > FINALIZE_POLL_S * FINALIZE_MAX_POLLS


def finalize_for_ingest_job(org_id, job_id) -> list[str]:
    """Finalize every running run whose output ingestion is `job_id` (called when that job finished)."""
    org = UUID(str(org_id))
    with tenant_step(org):
        run_ids = list(PipelineRun.objects.filter(ingest_job_id=job_id, status="running").values_list("id", flat=True))
    return [finalize_run(org_id=org, run_id=rid) for rid in run_ids]


def _notify_owner(run: PipelineRun, *, ok: bool) -> None:
    """In-app notification to the pipeline owner (inside the run's tenant step; best effort)."""
    from apps.datasets import services as datasets
    from apps.datasets.notifications import PIPELINE_RUN_FAILED, PIPELINE_RUN_SUCCEEDED

    pipeline = run.pipeline
    link = datasets.org_link(f"data/pipelines/{pipeline.id}/runs/{run.id}")
    if ok:
        rows = f"{run.rows_out:,} rows written" if run.rows_out is not None else "Finished"
        datasets.notify_users([pipeline.owner_id], PIPELINE_RUN_SUCCEEDED,
                              f"Pipeline '{pipeline.name}' run #{run.number} succeeded", f"{rows}.", link)
    else:
        datasets.notify_users([pipeline.owner_id], PIPELINE_RUN_FAILED,
                              f"Pipeline '{pipeline.name}' run #{run.number} failed",
                              f"{run.error_message} ({run.error_code}) after {run.attempt} attempt(s).", link)


def _succeed(run: PipelineRun, summary: dict) -> None:
    now = timezone.now()
    steps = list(run.steps or [])
    for s in steps:
        if s.get("type") == "destination.dataset":
            s["status"] = "succeeded"
            s["finished_at"] = now.isoformat()
            s["rows_out"] = summary.get("row_count")
            if s.get("started_at"):
                started = dt.datetime.fromisoformat(s["started_at"])
                s["duration_ms"] = int((now - started).total_seconds() * 1000)
    attempts = list(run.attempts or [])
    if attempts:
        attempts[-1] = {**attempts[-1], "finished_at": now.isoformat(), "error_code": None}
    run.status = "succeeded"
    run.finished_at = now
    run.duration_ms = int((now - run.started_at).total_seconds() * 1000) if run.started_at else None
    run.steps = steps
    run.attempts = attempts
    run.error_code, run.error_message, run.error_detail = "", "", None
    run.save()
    run_log(run, f"Run succeeded: {run.rows_out} rows, quality score {summary.get('score')}")
    result = {
        "run_id": str(run.id), "status": "succeeded", "rows_in": run.rows_in, "rows_out": run.rows_out,
        "rows_rejected": run.rows_rejected, "duration_ms": run.duration_ms,
        "output_dataset_id": str(run.output_dataset_id) if run.output_dataset_id else None,
        "output_dataset_version_id": str(run.output_dataset_version_id) if run.output_dataset_version_id else None,
        "quality_summary": summary,
    }
    audit.record("pipeline.run.succeeded", target=run.pipeline, metadata={**result, "attempt": run.attempt})
    if run.job_id:
        jobs.finish(run.job_id, result=result)
    _notify_owner(run, ok=True)
    from .signals import emit_run_succeeded_event

    ids = {"org_id": run.org_id, "run_id": run.id}
    transaction.on_commit(lambda: emit_run_succeeded_event(**ids))


def fail_or_retry(org: UUID, run_id, failure: RunFailure) -> str:
    """Record a failed attempt; re-queue with backoff when retryable and attempts remain, else fail the run."""
    from .tasks import execute_pipeline_run

    with tenant_step(org):
        run = PipelineRun.objects.select_for_update().select_related("pipeline").filter(id=run_id).first()
        if run is None or run.status in ("succeeded", "failed"):
            return "ignored"
        if failure.code == "cancelled" or run.status == "cancelled":
            if run.status != "cancelled":
                run.status = "cancelled"
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "finished_at", "updated_at"])
            return "cancelled"
        now = timezone.now()
        pipeline = run.pipeline
        attempts = list(run.attempts or [])
        if attempts:
            attempts[-1] = {**attempts[-1], "finished_at": now.isoformat(), "error_code": failure.code}
        run.attempts = attempts
        run.error_code = failure.code[:64]
        run.error_message = failure.message[:500]
        run.error_detail = {**failure.detail, **({"node": failure.node} if failure.node else {})} or None
        if engine.should_retry(failure, run.attempt, pipeline.max_retries):
            delay = engine.retry_delay_s(run.attempt, pipeline.retry_backoff_s, jitter=random.uniform(0, 0.1))
            failed_attempt = run.attempt
            run.attempt += 1
            run.status = "queued"
            run.next_attempt_at = now + dt.timedelta(seconds=delay)
            run.save()
            run_log(run, f"Attempt {failed_attempt} failed ({failure.code}): {failure.message} "
                         f"Retrying in {delay}s (attempt {run.attempt} of {run.max_attempts}).",
                    level="warning", context={"code": failure.code, "delay_s": delay})
            audit.record("pipeline.run.retry_scheduled", target=pipeline, metadata={
                "run_id": str(run.id), "failed_attempt": failed_attempt, "code": failure.code, "delay_s": delay})
            if run.job_id:
                jobs.update(run.job_id, status="running", message=f"Retrying in {delay}s after {failure.code}")
            kwargs = {"org_id": str(org), "run_id": str(run.id)}
            # thread runner: scheduling.dispatch_due starts the attempt once next_attempt_at is due
            enqueue_later(execute_pipeline_run, kwargs, delay)
            return "retrying"
        run.status = "failed"
        run.finished_at = now
        run.duration_ms = int((now - run.started_at).total_seconds() * 1000) if run.started_at else None
        run.save()
        run_log(run, f"Run failed ({failure.code}): {failure.message}", level="error",
                context={"code": failure.code, "node": failure.node})
        audit.record("pipeline.run.failed", target=pipeline, metadata={
            "run_id": str(run.id), "attempt": run.attempt, "code": failure.code, "message": failure.message,
            "node": failure.node})
        if run.job_id:
            jobs.fail(run.job_id, code=failure.code, message=failure.message,
                      detail={"run_id": str(run.id), "node": failure.node} if failure.node else {"run_id": str(run.id)})
        from .signals import notify_run_failed

        if pipeline.notify_on_failure:
            _notify_owner(run, ok=False)
        ids = {"org_id": org, "run_id": run.id, "pipeline_id": pipeline.id}
        transaction.on_commit(lambda: notify_run_failed(**ids))
        return "failed"
