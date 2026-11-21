"""Public service API of apps.ml. Other apps import only from here.

Projects     create_project / update_project / get_project / project_columns
EDA          start_eda (202 job) -> run_eda                  local statistics; optional Gemini narration (policy-gated)
Recommend    recommendations(project)                       ranked candidates with reasons (no training)
Snapshots    start_snapshot (202 job) -> run_snapshot        frozen, policy-filtered parquet + sha256
Experiments  start_experiments (202 job) -> run_training     TenantTask on queue "ml", job progress events
Compare      compare(ids)                                   side by side, best by the primary metric
Registry     register(experiment) / promote(model, v, stage)
Serving      predict_rows / predict_frame / start_batch_prediction / monitoring / run_drift_all
             (implemented in apps.ml.serving, re-exported here)

CONTRACT: `predict_frame(model_id, frame, *, version=None, explain=False) -> polars.DataFrame` and
`ModelNotReady` (raised when there is no production/staging version).
"""

from __future__ import annotations

import io
import logging
import time
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
import polars as pl
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs, storage
from apps.core.tenancy.context import require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import artifacts
from .data import governed_frame
from .engine import eda as eda_engine
from .engine import recommend as rec_engine
from .engine.algorithms import ALGORITHM_LABELS, InvalidHyperparameters, algorithms_for, validate_params
from .engine.frames import FEATURE_KINDS, column_profile, infer_kind, jsonable, to_pandas
from .engine.leakage import leakage_guard
from .engine.metrics import is_better
from .engine.training import TrainingError, TrainSpec, cap_rows, train
from .exceptions import (
    ArtifactRejected,
    MLInvalid,
    ModelNotReady,
    PolicyExcludedColumns,
    PredictionInputInvalid,
    ProjectNotReady,
)
from .models import DatasetSnapshot, Experiment, MLModel, MLProject, ModelVersion

log = logging.getLogger("nexora.ml")

__all__ = [
    "MLInvalid", "ModelNotReady", "PolicyExcludedColumns", "PredictionInputInvalid", "compare", "create_project", "get_experiment", "get_model",
    "get_project", "monitoring", "predict_frame", "predict_rows", "promote", "recommendations", "register",
    "run_drift_all", "start_batch_prediction", "start_eda", "start_experiments", "start_snapshot",
]

PROFILE_ROWS = 20_000
EDA_MAX_ROWS = 200_000
TEST_SIZE = 0.2
DEFAULT_RANDOM_STATE = 42
MAX_EXPERIMENTS_PER_REQUEST = 6


def _uuid(value, what: str = "id") -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise NotFound() from exc


def max_train_rows() -> int:
    return int(getattr(settings, "ML_MAX_TRAIN_ROWS", 500_000) or 500_000)


def max_train_seconds() -> int:
    return int(getattr(settings, "ML_MAX_TRAIN_SECONDS", 900) or 900)


def project_link(project_id) -> str:
    return f"/ml/projects/{project_id}"


def model_link(model_id) -> str:
    return f"/ml/models/{model_id}"


def job_links(org_slug: str, job) -> dict:
    base = f"/api/v1/orgs/{org_slug}/jobs/{job.id}"
    return {"job_id": str(job.id), "status_url": base, "events_url": f"{base}/events"}


def _notify(user_ids, kind: str, title: str, body: str, link: str) -> None:
    try:
        from apps.core.notifications import notify
    except ImportError:  # pragma: no cover - core notifications exist from W3
        return
    try:
        with transaction.atomic():
            notify([u for u in user_ids if u], kind, title[:200], body, link)
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.warning("ml: notification %s failed", kind, exc_info=True)


# ------------------------------------------------------------------------------------------------ lookups
def get_project(pk) -> MLProject:
    obj = MLProject.objects.filter(id=_uuid(pk)).first()
    if obj is None:
        raise NotFound()
    return obj


def get_experiment(pk) -> Experiment:
    obj = Experiment.objects.select_related("project", "snapshot").filter(id=_uuid(pk)).first()
    if obj is None:
        raise NotFound()
    return obj


def get_model(pk) -> MLModel:
    obj = MLModel.objects.select_related("project").filter(id=_uuid(pk)).first()
    if obj is None:
        raise NotFound()
    return obj


def get_version(model: MLModel, version: int) -> ModelVersion:
    obj = ModelVersion.objects.filter(model=model, version=int(version)).first()
    if obj is None:
        raise NotFound()
    return obj


# ------------------------------------------------------------------------------------------------ projects
def _kinds_for(frame: pd.DataFrame) -> dict[str, str]:
    return {c: infer_kind(c, frame[c]) for c in frame.columns}


def _class_stats(frame: pd.DataFrame, target: str) -> tuple[int, float | None]:
    counts = frame[target].dropna().astype(str).value_counts()
    if not len(counts):
        return 0, None
    return int(len(counts)), float(counts.min() / counts.sum())


def create_project(*, user_id, data: dict, request=None) -> MLProject:
    """Validate target/features against the user's policy, run the leakage guard and detect the task."""
    dataset_id = data["dataset_id"]
    target = (data.get("target") or "").strip()
    time_column = (data.get("time_column") or "").strip()
    requested_task = data.get("task_type") or ""
    requested_features = data.get("features")
    gf = governed_frame(user_id, dataset_id, limit=PROFILE_ROWS)
    usable = set(gf.allowed_columns)
    excluded_names = {e["column"] for e in gf.excluded_by_policy}
    errors: dict[str, list[str]] = {}
    blocked = []
    for field_name, col in (("target", target), ("time_column", time_column)):
        if not col:
            continue
        if col in excluded_names:
            blocked.append(col)
            errors[field_name] = ["Your data access policy does not allow this column."]
        elif col not in gf.all_columns:
            errors[field_name] = ["Unknown column."]
    if requested_features is not None:
        unknown = [c for c in requested_features if c not in gf.all_columns]
        denied = [c for c in requested_features if c in excluded_names]
        if unknown:
            errors["features"] = [f"Unknown columns: {', '.join(unknown)}."]
        if denied:
            blocked.extend(denied)
            errors.setdefault("features", []).append(
                f"Not allowed by your data access policy: {', '.join(denied)}.")
    if errors:
        raise ValidationError({**errors, "excluded_by_policy": sorted(set(blocked))})
    frame = gf.pandas()
    kinds = _kinds_for(frame)
    if time_column and kinds.get(time_column) != "datetime":
        raise ValidationError({"time_column": ["This column does not contain dates or timestamps."]})
    task = requested_task or eda_engine.detect_task(frame, target or None, kinds, time_column=time_column or None)
    if task not in eda_engine.TASK_TYPES:
        raise ValidationError({"task_type": ["Unknown task type."]})
    if task != "clustering" and not target:
        raise ValidationError({"target": ["A target column is required for this task."]})
    if task == "forecast" and not time_column:
        raise ValidationError({"time_column": ["Forecasting needs a time column."]})
    if task in ("regression", "forecast") and target and kinds.get(target) not in ("numeric", "boolean"):
        raise ValidationError({"target": [f"A {task} target must be numeric."]})
    if task == "clustering":
        target = ""
    n_classes, minority = (None, None)
    if task == "classification":
        n_classes, minority = _class_stats(frame, target)
        if n_classes < 2:
            raise ValidationError({"target": ["The target needs at least two classes."]})
        if n_classes > 50:
            raise ValidationError({"target": [f"The target has {n_classes} classes; classification supports "
                                              f"at most 50."]})
    candidates = [c for c in (requested_features if requested_features is not None else gf.allowed_columns)
                  if c in usable]
    guard = leakage_guard(frame, target or None, candidates, kinds, time_column=time_column or None)
    features = guard["features"] if task != "forecast" else []
    if task not in ("forecast",) and not features:
        raise ValidationError({"features": ["No usable feature columns remain after the leakage guard."],
                               "leakage": guard["dropped"]})
    primary = data.get("primary_metric") or rec_engine.primary_metric_for(task, n_classes=n_classes,
                                                                          minority_share=minority)
    profile = {
        "dataset_name": gf.dataset_name, "dataset_version": gf.version_no, "classification": gf.classification,
        "rows_profiled": int(len(frame)), "source_rows": int(gf.source_rows), "row_filtered": gf.row_filtered,
        "n_classes": n_classes, "minority_share": minority,
        "columns": [column_profile(c, frame[c], kinds[c]) for c in frame.columns],
    }
    project = MLProject.objects.create(
        name=data["name"].strip()[:200], description=data.get("description", ""),
        business_goal=data.get("business_goal", ""), dataset_id=gf.dataset_id, target=target, task_type=task,
        task_type_source="user" if requested_task else "auto", time_column=time_column, features=features,
        column_kinds={c: kinds[c] for c in frame.columns}, excluded_by_policy=gf.excluded_by_policy,
        leakage=guard["dropped"], primary_metric=primary, profile=jsonable(profile), created_by=user_id)
    audit.record("ml.project.create", target=project, request=request, metadata={
        "dataset_id": str(gf.dataset_id), "task_type": task, "target": target, "features": len(features),
        "excluded_by_policy": len(gf.excluded_by_policy), "leakage_dropped": len(guard["dropped"])})
    return project


def update_project(project: MLProject, data: dict, *, request=None) -> MLProject:
    changes = {}
    for f in ("name", "description", "business_goal", "primary_metric", "status"):
        if f in data and getattr(project, f) != data[f]:
            changes[f] = {"before": getattr(project, f), "after": data[f]}
            setattr(project, f, data[f])
    if "features" in data:
        new = list(dict.fromkeys(data["features"]))
        allowed = set(project.features) | {c["column"] for c in project.leakage if c["reason"] not in
                                           ("id_like", "identical_to_target", "near_duplicate_of_target")}
        bad = [c for c in new if c not in allowed or project.column_kinds.get(c) not in FEATURE_KINDS]
        if bad:
            raise ValidationError({"features": [f"Not selectable: {', '.join(bad)} (leakage, policy or type)."]})
        if not new and project.task_type != "forecast":
            raise ValidationError({"features": ["Select at least one feature."]})
        changes["features"] = {"before": project.features, "after": new}
        project.features = new
    if changes:
        project.save()
        audit.record("ml.project.update", target=project, request=request, changes=changes)
    return project


# ------------------------------------------------------------------------------------------------ EDA
def start_eda(project: MLProject, *, user_id, narrate: bool = False, request=None):
    if project.eda_status == "running":
        raise ProjectNotReady("EDA is already running for this project.")
    job = jobs.create_job("ml_eda", user_id=user_id, message="Queued")
    project.eda_status, project.eda_job_id = "running", job.id
    project.save(update_fields=["eda_status", "eda_job_id", "updated_at"])
    audit.record("ml.project.eda", target=project, request=request, metadata={"job_id": str(job.id),
                                                                               "narrate": bool(narrate)})
    from .tasks import run_eda_task

    kwargs = {"org_id": str(require_org_id()), "project_id": str(project.id), "job_id": str(job.id),
              "user_id": str(user_id) if user_id else None, "narrate": bool(narrate),
              "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: run_eda_task.delay(**kwargs))
    return job


def narrate(facts: dict, *, dataset_id, user_id=None, kind: str = "eda") -> tuple[str | None, str | None]:
    """(text, None) from Gemini, or (None, reason) when the policy/quota/availability says no."""
    from apps.ai.exceptions import AIInvalidOutput, AIPolicyBlocked, AIQuotaExhausted, AIUnavailable
    from apps.ai.services import gemini, get_prompt
    from nexora.contracts import PayloadClass, PayloadManifest

    spec = get_prompt("ml.narrate")
    ref = (f"dataset:{dataset_id}",)
    attempts = [
        (PayloadManifest({PayloadClass.SCHEMA: ref, PayloadClass.VALUES: ref}), facts),
        # schema-only fallback: column names and the task, no values at all
        (PayloadManifest({PayloadClass.SCHEMA: ref}),
         {"task": facts.get("task"), "target": facts.get("target"),
          "columns": [a.get("feature") for a in facts.get("top_associations", [])]}),
    ]
    reason = None
    for payload, body in attempts:
        try:
            text = gemini().generate_text(feature="ml.narrate", prompt_key=spec.key, system=spec.system,
                                          prompt=spec.render(facts=body, kind=kind), payload=payload,
                                          tier=spec.tier, user_id=user_id, temperature=spec.temperature,
                                          max_output_tokens=spec.max_output_tokens)
            return (text or "").strip() or None, None
        except AIPolicyBlocked:
            reason = "policy_blocked"
            continue
        except AIQuotaExhausted:
            return None, "quota_exhausted"
        except (AIUnavailable, AIInvalidOutput):
            return None, "ai_unavailable"
    return None, reason


def run_eda(*, org_id, project_id, job_id, user_id=None, narrate_flag: bool = False) -> dict | None:
    org = _uuid(org_id)
    try:
        with tenant_step(org):
            project = MLProject.objects.get(id=project_id)
            jobs.update(job_id, status="running", progress=10, message="Loading data")
            cols = list(dict.fromkeys([*project.features, *([project.target] if project.target else []),
                                       *([project.time_column] if project.time_column else [])]))
            gf = governed_frame(user_id or project.created_by, project.dataset_id, columns=cols,
                                require=[c for c in (project.target, project.time_column) if c])
        frame = gf.frame
        sampled = frame.height > EDA_MAX_ROWS
        if sampled:
            frame = frame.sample(EDA_MAX_ROWS, seed=DEFAULT_RANDOM_STATE)
        df = to_pandas(frame)
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=40, message="Computing statistics")
        kinds = {c: project.column_kinds.get(c) or infer_kind(c, df[c]) for c in df.columns}
        result = eda_engine.compute_eda(df, target=project.target or None, task=project.task_type,
                                        features=[f for f in project.features if f in df.columns], kinds=kinds,
                                        time_column=project.time_column or None)
        result["sampled"] = {"sampled": sampled, "rows": int(len(df)), "source_rows": int(gf.frame.height)}
        result["excluded_by_policy"] = gf.excluded_by_policy
        text = eda_engine.template_narrative(result, task=project.task_type, target=project.target or None)
        source, reason = "template", None
        if narrate_flag:
            with tenant_step(org):
                jobs.update(job_id, status="running", progress=80, message="Writing the explanation")
                ai_text, reason = narrate(eda_engine.narration_facts(result, task=project.task_type,
                                                                     target=project.target or None),
                                          dataset_id=project.dataset_id, user_id=user_id)
            if ai_text:
                text, source = ai_text, "gemini"
        with tenant_step(org):
            MLProject.objects.filter(id=project_id).update(
                eda=jsonable(result), eda_status="ready", eda_narrative=text, eda_narrative_source=source,
                updated_at=timezone.now())
            summary = {"project_id": str(project_id), "rows": result["rows"], "warnings": result["warnings"],
                       "narrative_source": source, "narration_skipped_reason": reason}
            jobs.finish(job_id, result=summary)
        return summary
    except Exception as exc:  # noqa: BLE001 - mark failed; traceback to the worker log
        log.exception("ml: EDA failed for project %s", project_id)
        code = getattr(exc, "default_code", None) or "ml_eda_failed"
        with tenant_step(org):
            MLProject.objects.filter(id=project_id).update(eda_status="failed", updated_at=timezone.now())
            jobs.fail(job_id, code=str(code), message=_error_message(exc, "The analysis failed."))
        return None


def _error_message(exc: Exception, default: str) -> str:
    if isinstance(exc, TrainingError):
        return exc.message
    detail = getattr(exc, "detail", None)
    if isinstance(detail, str):
        return detail
    return f"{default} ({type(exc).__name__})"


# ------------------------------------------------------------------------------------------ recommendations
def recommendations(project: MLProject) -> dict:
    eda = project.eda or {}
    prof = project.profile or {}
    n_rows = int(eda.get("rows") or prof.get("source_rows") or prof.get("rows_profiled") or 0)
    target = eda.get("target") or {}
    n_classes = target.get("n_classes") or prof.get("n_classes")
    minority = target.get("minority_share", prof.get("minority_share"))
    periods = season = None
    if project.task_type == "forecast":
        tc = eda.get("time_coverage") or {}
        periods = tc.get("periods")
        from .engine.forecast import season_length

        if tc.get("frequency") and periods:
            season = season_length(tc["frequency"], periods)
    out = rec_engine.recommend(task=project.task_type, n_rows=n_rows, kinds=project.column_kinds,
                               features=project.features, business_goal=f"{project.business_goal} "
                                                                         f"{project.description}",
                               n_classes=n_classes, minority_share=minority, periods=periods,
                               season_length=season)
    out["based_on"] = "eda" if project.eda else "profile"
    out["rows"] = n_rows
    out["primary_metric"] = project.primary_metric or out["primary_metric"]
    return out


# ------------------------------------------------------------------------------------------------ snapshots
def _new_snapshot(project: MLProject, user_id, *, test_size: float, random_state: int) -> DatasetSnapshot:
    return DatasetSnapshot.objects.create(
        project=project, dataset_id=project.dataset_id, created_by=user_id, status="building",
        split_spec={"kind": "time_ordered" if project.time_column else (
            "stratified" if project.task_type == "classification" else "random"),
            "test_size": test_size, "random_state": random_state})


def start_snapshot(project: MLProject, *, user_id, data: dict | None = None, request=None):
    data = data or {}
    snap = _new_snapshot(project, user_id, test_size=float(data.get("test_size") or TEST_SIZE),
                         random_state=int(data.get("random_state", DEFAULT_RANDOM_STATE)))
    job = jobs.create_job("ml_snapshot", user_id=user_id, message="Queued")
    snap.job_id = job.id
    snap.save(update_fields=["job_id", "updated_at"])
    audit.record("ml.snapshot.create", target=snap, request=request, metadata={"project_id": str(project.id),
                                                                               "job_id": str(job.id)})
    from .tasks import run_snapshot_task

    kwargs = {"org_id": str(require_org_id()), "snapshot_id": str(snap.id), "job_id": str(job.id),
              "user_id": str(user_id) if user_id else None, "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: run_snapshot_task.delay(**kwargs))
    return snap, job


def build_snapshot(org: UUID, snapshot_id, user_id) -> DatasetSnapshot:
    """Freeze the policy-filtered training frame: parquet under orgs/<org>/ml/snapshots/<id>.parquet + sha256."""
    with tenant_step(org):
        snap = DatasetSnapshot.objects.select_related("project").get(id=snapshot_id)
        project = snap.project
        cols = list(dict.fromkeys([*project.features, *([project.target] if project.target else []),
                                   *([project.time_column] if project.time_column else [])]))
        gf = governed_frame(user_id or project.created_by, project.dataset_id, columns=cols, require=cols)
    frame = gf.frame
    if project.target:
        frame = frame.filter(pl.col(project.target).is_not_null())
    source_rows = frame.height
    sample_info: dict[str, Any] = {"sampled": False, "source_rows": source_rows, "rows": source_rows}
    cap = max_train_rows()
    if source_rows > cap:
        pdf = to_pandas(frame)
        strat = pdf[project.target] if project.task_type == "classification" else None
        sampled, sample_info = cap_rows(pdf, cap, stratify=strat,
                                        random_state=int(snap.split_spec.get("random_state", DEFAULT_RANDOM_STATE)))
        frame = frame.with_row_index("__row").filter(pl.col("__row").is_in(sampled.index.tolist())).drop("__row")
    buf = io.BytesIO()
    frame.write_parquet(buf, compression="zstd", statistics=False)
    data = buf.getvalue()
    with storage.org_scope(org):
        key = artifacts.snapshot_key(snapshot_id)
        digest = artifacts.save_bytes(key, data, content_type="application/vnd.apache.parquet")
    with tenant_step(org):
        DatasetSnapshot.objects.filter(id=snapshot_id).update(
            dataset_version_id=gf.version_id, dataset_version=gf.version_no, columns=list(frame.columns),
            excluded_by_policy=gf.excluded_by_policy, row_count=frame.height, source_row_count=gf.source_rows,
            sha256=digest, storage_key=key, sample_info=jsonable(sample_info), status="ready", error="",
            updated_at=timezone.now())
        return DatasetSnapshot.objects.get(id=snapshot_id)


def load_snapshot_frame(snap: DatasetSnapshot) -> pd.DataFrame:
    """The frozen frame, verified against its sha256 (a tampered snapshot is refused)."""
    data = artifacts.read_verified_bytes(snap.storage_key, snap.sha256)
    return to_pandas(pl.read_parquet(io.BytesIO(data)))


def run_snapshot(*, org_id, snapshot_id, job_id, user_id=None) -> dict | None:
    org = _uuid(org_id)
    try:
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=10, message="Freezing the training data")
        snap = build_snapshot(org, snapshot_id, user_id)
        with tenant_step(org):
            result = {"snapshot_id": str(snap.id), "rows": snap.row_count, "sha256": snap.sha256,
                      "sample_info": snap.sample_info}
            audit.record("ml.snapshot.ready", target=snap, metadata=result)
            jobs.finish(job_id, result=result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("ml: snapshot %s failed", snapshot_id)
        with tenant_step(org):
            msg = _error_message(exc, "The snapshot could not be built.")
            DatasetSnapshot.objects.filter(id=snapshot_id).update(status="failed", error=msg[:2000],
                                                                   updated_at=timezone.now())
            jobs.fail(job_id, code=str(getattr(exc, "default_code", "ml_snapshot_failed")), message=msg)
        return None


# ------------------------------------------------------------------------------------------------ experiments
def start_experiments(project: MLProject, *, user_id, data: dict, request=None):
    if project.status != "active":
        raise ProjectNotReady("This project is archived.")
    algos = list(dict.fromkeys(data.get("algorithms") or ([data["algorithm"]] if data.get("algorithm") else [])))
    recs = recommendations(project)
    ranks = {c["algorithm"]: c["rank"] for c in recs["candidates"]}
    if not algos:
        algos = [c["algorithm"] for c in recs["candidates"]][:3]
    if len(algos) > MAX_EXPERIMENTS_PER_REQUEST:
        raise ValidationError({"algorithms": [f"At most {MAX_EXPERIMENTS_PER_REQUEST} per request."]})
    allowed = algorithms_for(project.task_type)
    bad = [a for a in algos if a not in allowed]
    if bad:
        raise ValidationError({"algorithms": [f"Not available for {project.task_type}: {', '.join(bad)}. "
                                              f"Choose from {', '.join(allowed)}."]})
    hyper = data.get("hyperparameters") or {}
    per_algo = isinstance(hyper, dict) and hyper and all(k in allowed for k in hyper)
    params_for = {a: (hyper.get(a, {}) if per_algo else hyper) for a in algos}
    errors = {}
    for a, p in params_for.items():
        try:
            validate_params(a, p)
        except InvalidHyperparameters as exc:
            errors[a] = exc.errors
    if errors:
        raise ValidationError({"hyperparameters": errors})
    random_state = int(data.get("random_state", DEFAULT_RANDOM_STATE))
    snapshot_id = data.get("snapshot_id")
    if snapshot_id:
        snap = DatasetSnapshot.objects.filter(id=_uuid(snapshot_id), project=project).first()
        if snap is None:
            raise ValidationError({"snapshot_id": ["Unknown snapshot for this project."]})
        if snap.status == "failed":
            raise ValidationError({"snapshot_id": ["This snapshot failed; create a new one."]})
    else:
        snap = _new_snapshot(project, user_id, test_size=TEST_SIZE, random_state=random_state)
    job = jobs.create_job("ml_train", user_id=user_id, message="Queued")
    override = (data.get("override_reason") or "").strip()
    created = []
    for a in algos:
        created.append(Experiment.objects.create(
            project=project, snapshot=snap, name=(data.get("name") or ALGORITHM_LABELS[a])[:200], algorithm=a,
            task_type=project.task_type, hyperparameters=params_for[a], random_state=random_state,
            tune=bool(data.get("tune", False)), status="queued", primary_metric=project.primary_metric,
            recommended=a in ranks, recommendation_rank=ranks.get(a),
            override_reason=override if a not in ranks or ranks.get(a) != 1 else "", job_id=job.id,
            created_by=user_id))
    if snap.job_id is None:
        snap.job_id = job.id
        snap.save(update_fields=["job_id", "updated_at"])
    audit.record("ml.experiment.create", target=project, request=request, metadata={
        "job_id": str(job.id), "experiments": [str(e.id) for e in created], "algorithms": algos,
        "random_state": random_state, "snapshot_id": str(snap.id), "override_reason": override or None,
        "recommended_first": recs["candidates"][0]["algorithm"] if recs["candidates"] else None})
    from .tasks import run_training_task

    kwargs = {"org_id": str(require_org_id()), "job_id": str(job.id), "experiment_ids": [str(e.id) for e in created],
              "snapshot_id": str(snap.id), "user_id": str(user_id) if user_id else None,
              "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: run_training_task.delay(**kwargs))
    return created, job, snap


def _spec_for(exp: Experiment, project: MLProject, snap: DatasetSnapshot, budget_s: float) -> TrainSpec:
    return TrainSpec(task=project.task_type, algorithm=exp.algorithm,
                     features=[f for f in project.features if f in snap.columns],
                     kinds=dict(project.column_kinds), target=project.target or None, params=exp.hyperparameters,
                     random_state=exp.random_state, time_column=project.time_column or None,
                     test_size=float(snap.split_spec.get("test_size", TEST_SIZE)), time_budget_s=budget_s,
                     tune=exp.tune, primary_metric=project.primary_metric or None, max_rows=max_train_rows())


def _save_experiment_result(exp_id, result, key: str, digest: str, size: int) -> None:
    Experiment.objects.filter(id=exp_id).update(
        status="succeeded", metrics=result.metrics, primary_metric=result.metrics.get("primary_metric", ""),
        primary_value=result.metrics.get("primary_value"), effective_params=result.params,
        confusion_matrix=result.confusion_matrix, residuals=result.residuals, curves=result.curves,
        feature_importances=result.feature_importances, shap_summary=result.shap_summary,
        reference_stats=result.reference_stats, feature_schema=jsonable(result.bundle.feature_schema),
        warnings=result.warnings, sample_info=jsonable(result.sample_info), train_rows=result.train_rows,
        test_rows=result.test_rows, duration_s=result.duration_s, artifact_key=key, artifact_sha256=digest,
        artifact_size=size, lib_versions=result.bundle.versions, finished_at=timezone.now(), error="",
        error_code="", updated_at=timezone.now())


def run_training(*, org_id, job_id, experiment_ids, snapshot_id, user_id=None) -> dict | None:
    """Queue "ml". Snapshot (if still building) -> each experiment in turn -> artifacts -> results."""
    org = _uuid(org_id)
    n = max(len(experiment_ids), 1)
    outcome = []
    try:
        with tenant_step(org):
            snap = DatasetSnapshot.objects.select_related("project").get(id=snapshot_id)
            jobs.update(job_id, status="running", progress=2, message="Preparing training data")
        if snap.status != "ready":
            snap = build_snapshot(org, snapshot_id, user_id)
        with tenant_step(org):
            snap = DatasetSnapshot.objects.select_related("project").get(id=snapshot_id)
            project = snap.project
            df = load_snapshot_frame(snap)
    except Exception as exc:  # noqa: BLE001
        log.exception("ml: training data for job %s failed", job_id)
        msg = _error_message(exc, "The training data could not be prepared.")
        with tenant_step(org):
            DatasetSnapshot.objects.filter(id=snapshot_id, status="building").update(status="failed", error=msg)
            Experiment.objects.filter(id__in=experiment_ids).update(status="failed", error=msg,
                                                                    error_code="ml_snapshot_failed",
                                                                    finished_at=timezone.now())
            jobs.fail(job_id, code="ml_snapshot_failed", message=msg)
        return None

    budget = max_train_seconds()
    for i, exp_id in enumerate(experiment_ids):
        base = 10 + 85 * i / n
        span = 85 / n
        with tenant_step(org):
            exp = Experiment.objects.get(id=exp_id)
            Experiment.objects.filter(id=exp_id).update(status="running", started_at=timezone.now())
            jobs.update(job_id, status="running", progress=round(base, 1),
                        message=f"Training {ALGORITHM_LABELS.get(exp.algorithm, exp.algorithm)} ({i + 1}/{n})")
            jobs.publish(job_id, "log", {"message": f"experiment {exp_id} started", "experiment_id": str(exp_id)})

        def progress(pct: float, msg: str, _base=base, _span=span, _algo=exp.algorithm) -> None:
            try:
                with tenant_step(org):
                    jobs.update(job_id, status="running", progress=round(_base + _span * pct / 100, 1),
                                message=f"{ALGORITHM_LABELS.get(_algo, _algo)}: {msg}")
            except Exception:  # noqa: BLE001 - progress is best effort
                log.debug("ml: progress update failed", exc_info=True)

        try:
            started = time.monotonic()
            result = train(df, _spec_for(exp, project, snap, budget), progress=progress)
            with storage.org_scope(org):
                key = artifacts.experiment_key(exp_id)
                digest, size = artifacts.save_bundle(key, result.bundle)
            with tenant_step(org):
                _save_experiment_result(exp_id, result, key, digest, size)
                audit.record("ml.experiment.train", target_type="ml.experiment", target_id=str(exp_id),
                             metadata={"algorithm": exp.algorithm, "primary_metric": result.metrics.get(
                                 "primary_metric"), "primary_value": result.metrics.get("primary_value"),
                                 "duration_s": round(time.monotonic() - started, 2), "random_state": exp.random_state,
                                 "sha256": digest})
            outcome.append({"experiment_id": str(exp_id), "algorithm": exp.algorithm, "status": "succeeded",
                            "primary_value": result.metrics.get("primary_value")})
        except Exception as exc:  # noqa: BLE001 - one failed experiment never stops the others
            if not isinstance(exc, TrainingError | InvalidHyperparameters | ValueError):
                log.exception("ml: experiment %s failed", exp_id)
            msg = _error_message(exc, "Training failed.")
            code = exc.code if isinstance(exc, TrainingError) else "ml_training_failed"
            with tenant_step(org):
                Experiment.objects.filter(id=exp_id).update(status="failed", error=msg[:2000], error_code=code[:60],
                                                            finished_at=timezone.now(), updated_at=timezone.now())
                jobs.publish(job_id, "log", {"message": f"experiment {exp_id} failed: {msg}",
                                             "experiment_id": str(exp_id)})
            outcome.append({"experiment_id": str(exp_id), "algorithm": exp.algorithm, "status": "failed",
                            "error": msg})

    with tenant_step(org):
        ok = [o for o in outcome if o["status"] == "succeeded"]
        best = _best(ok, project.primary_metric)
        result = {"project_id": str(project.id), "snapshot_id": str(snapshot_id), "experiments": outcome,
                  "best_experiment_id": best}
        if ok:
            jobs.finish(job_id, result=result)
            _notify([user_id], "ml.training_finished", f"Training finished: {project.name}",
                    f"{len(ok)} of {len(outcome)} experiment(s) succeeded.", project_link(project.id))
        else:
            jobs.fail(job_id, code="ml_training_failed", message="Every experiment failed.", detail=result)
            _notify([user_id], "ml.training_failed", f"Training failed: {project.name}",
                    outcome[0].get("error", "") if outcome else "", project_link(project.id))
    return result


def _best(items: list[dict], metric: str) -> str | None:
    best = None
    for item in items:
        if best is None or is_better(metric, item.get("primary_value"), best.get("primary_value")):
            best = item
    return best["experiment_id"] if best else None


# ------------------------------------------------------------------------------------------------ compare
COMPARE_KEYS = {
    "classification": ("accuracy", "precision_macro", "recall_macro", "f1_macro", "f1_weighted", "roc_auc", "pr_auc",
                       "log_loss"),
    "regression": ("mae", "rmse", "r2", "mape"),
    "forecast": ("mae", "rmse", "mape", "smape", "baseline_mae", "mae_vs_baseline", "interval_coverage"),
    "clustering": ("silhouette", "davies_bouldin", "calinski_harabasz", "n_clusters"),
}


def compare(ids: list) -> dict:
    uuids = [_uuid(i) for i in ids]
    exps = list(Experiment.objects.select_related("project").filter(id__in=uuids))
    if len(exps) != len(set(uuids)):
        raise NotFound()
    tasks = {e.task_type for e in exps}
    if len(tasks) > 1:
        raise ValidationError({"ids": ["Only experiments of the same task type can be compared."]})
    task = tasks.pop() if tasks else "classification"
    metrics_keys = COMPARE_KEYS[task]
    primary = exps[0].project.primary_metric if exps else ""
    if len({e.project.primary_metric for e in exps}) > 1:
        primary = rec_engine.primary_metric_for(task)
    items = []
    for e in sorted(exps, key=lambda x: uuids.index(x.id)):
        test = (e.metrics or {}).get("test") or {}
        cv = (e.metrics or {}).get("cv") or {}
        items.append({"id": str(e.id), "name": e.name, "algorithm": e.algorithm,
                      "label": ALGORITHM_LABELS.get(e.algorithm, e.algorithm), "status": e.status,
                      "project_id": str(e.project_id), "random_state": e.random_state,
                      "metrics": {k: test.get(k) for k in metrics_keys},
                      "primary_value": test.get(primary), "cv_mean": cv.get("mean"), "cv_std": cv.get("std"),
                      "duration_s": e.duration_s, "train_rows": e.train_rows,
                      "top_features": [f["feature"] for f in ((e.shap_summary or {}).get("features") or [])[:5]],
                      "recommended": e.recommended})
    ok = [i for i in items if i["status"] == "succeeded"]
    ranking = sorted(ok, key=lambda i: (0 if i["primary_value"] is not None else 1,
                                        -(i["primary_value"] or 0) if is_better(primary, 1.0, 0.0)
                                        else (i["primary_value"] or 0),
                                        rec_engine.SIMPLICITY.get(i["algorithm"], 9)))
    return {"task_type": task, "primary_metric": primary, "metrics": list(metrics_keys), "items": items,
            "best_id": ranking[0]["id"] if ranking else None, "ranking": [i["id"] for i in ranking]}


# ------------------------------------------------------------------------------------------------ registry
def register(experiment: Experiment, *, user_id, data: dict | None = None, request=None) -> ModelVersion:
    data = data or {}
    if experiment.status != "succeeded" or not experiment.artifact_key:
        raise ProjectNotReady("Only a succeeded experiment with an artifact can be registered.")
    project = experiment.project
    model = None
    if data.get("model_id"):
        model = MLModel.objects.filter(id=_uuid(data["model_id"])).first()
        if model is None:
            raise ValidationError({"model_id": ["Unknown model."]})
    else:
        name = (data.get("model_name") or project.name).strip()[:200]
        model = MLModel.objects.filter(name=name).first()
        if model is None:
            model = MLModel.objects.create(project=project, name=name, description=data.get("description", ""),
                                           task_type=project.task_type, target=project.target, created_by=user_id)
            audit.record("ml.model.create", target=model, request=request, metadata={"project_id": str(project.id)})
    if model.project_id != project.id:
        raise ValidationError({"model_name": ["A model with this name belongs to another project."]})
    data_bytes = artifacts.read_verified_bytes(experiment.artifact_key, experiment.artifact_sha256)
    with transaction.atomic():
        MLModel.objects.select_for_update().filter(id=model.id).first()
        next_version = (ModelVersion.objects.filter(model=model).aggregate(m=Max("version"))["m"] or 0) + 1
        key = artifacts.model_key(model.id, next_version)
        digest = artifacts.save_bytes(key, data_bytes)
        if digest != experiment.artifact_sha256:
            raise ArtifactRejected("The registered artifact does not match the experiment artifact.")
        snap = experiment.snapshot
        version = ModelVersion.objects.create(
            model=model, version=next_version, experiment=experiment, algorithm=experiment.algorithm,
            artifact_key=key, artifact_sha256=digest, artifact_size=len(data_bytes),
            feature_schema=experiment.feature_schema, metrics=experiment.metrics,
            reference_stats=experiment.reference_stats, lib_versions=experiment.lib_versions,
            dataset_id=project.dataset_id, dataset_version_id=snap.dataset_version_id if snap else None,
            snapshot_id=snap.id if snap else None, stage="none", created_by=user_id)
    if snap and snap.dataset_version_id:
        try:
            from apps.lineage.services import record as lineage_record

            with transaction.atomic():
                lineage_record("dataset_version", snap.dataset_version_id, "ml_model_version", version.id,
                               label=f"trained {experiment.algorithm}")
        except Exception:  # noqa: BLE001 - lineage is best effort
            log.warning("ml: lineage edge for version %s not recorded", version.id, exc_info=True)
    audit.record("ml.model.register", target=version, request=request, metadata={
        "model_id": str(model.id), "version": next_version, "experiment_id": str(experiment.id),
        "sha256": digest, "algorithm": experiment.algorithm})
    return version


PROMOTE_STAGES = ("staging", "production", "archived")


def promote(model: MLModel, version_no: int, stage: str, *, user_id, request=None) -> ModelVersion:
    """CONTRACT "Promotion (W4)": one production version per model; the previous one becomes archived."""
    if stage not in PROMOTE_STAGES:
        raise ValidationError({"stage": [f"Use one of {', '.join(PROMOTE_STAGES)}."]})
    with transaction.atomic():
        versions = {v.version: v for v in ModelVersion.objects.select_for_update().filter(model=model)}
        version = versions.get(int(version_no))
        if version is None:
            raise NotFound()
        before = version.stage
        demoted = []
        if stage == "production":
            for other in versions.values():
                if other.id != version.id and other.stage == "production":
                    other.stage = "archived"
                    other.save(update_fields=["stage", "updated_at"])
                    demoted.append(other.version)
        version.stage = stage
        version.promoted_by = user_id
        version.promoted_at = timezone.now()
        version.save(update_fields=["stage", "promoted_by", "promoted_at", "updated_at"])
    audit.record("ml.model.promote", target=version, request=request,
                 changes={"before": {"stage": before}, "after": {"stage": stage}},
                 metadata={"model_id": str(model.id), "version": version.version, "archived_versions": demoted})
    return version


# --------------------------------------------------------------------------------------- serving re-exports
from .serving import (  # noqa: E402
    monitoring,
    predict_frame,
    predict_rows,
    run_drift_all,
    serving_version,
    start_batch_prediction,
)

__all__ += ["serving_version"]
_np = np  # numpy is part of the public frame contract (re-exported for type hints in callers)
