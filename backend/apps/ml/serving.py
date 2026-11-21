"""Model serving and monitoring (ML_ARCHITECTURE §8-§9, CONTRACT W4).

    serving_version(model, version)       which version answers: explicit > production > staging, else ModelNotReady
    predict_rows(...)                     POST /ml/models/{id}/predict (JSON rows <= 1000, optional SHAP drivers)
    predict_frame(model_id, frame, ...)   CONTRACT cross-app service (C360 scoring): polars in -> polars out
    start_batch_prediction / run_batch_prediction
                                          202 job: score a dataset the caller may read and publish the result as a
                                          new dataset via apps.datasets.services.publish_frame (lineage source "ml")
    monitoring(model)                     drift (PSI vs the training reference) + volume + label performance
    run_drift(model) / run_drift_all()    persisted DriftReports; run_drift_all is the periodic job "ml.drift"

Prediction logs keep the sha256 of the canonical input and per-feature COUNTS in the reference bins, never raw rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
import polars as pl
from django.db import DatabaseError, connections, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDay
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.tenancy.context import current_actor, require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import artifacts
from .data import allowed_columns, governed_frame
from .engine import drift as drift_engine
from .engine.bundle import ModelBundle
from .engine.explain import MAX_EXPLAIN_ROWS, explain_rows
from .engine.frames import jsonable, to_pandas
from .engine.metrics import classification_metrics, regression_metrics
from .engine.validation import PredictionInputError, validate_frame, validate_rows
from .exceptions import MLInvalid, ModelNotReady, PolicyExcludedColumns, PredictionInputInvalid
from .models import DriftReport, MLModel, ModelVersion, PredictionLog

log = logging.getLogger("nexora.ml")

PREDICTION_COLUMN = "prediction"
PROBABILITY_COLUMN = "probability"
EXPLANATION_COLUMN = "explanation"
FRAME_CHUNK = 20_000
MAX_FORECAST_HORIZON = 366
MONITORING_WINDOW_DAYS = 7
DRIFT_PERIODIC_NAME = "ml.drift"
DRIFT_EVERY_S = 3600
DRIFT_ORG_SCAN_SQL = ("SELECT DISTINCT org_id FROM ml_modelversion WHERE stage IN ('production', 'staging') "
                      "LIMIT 10000")
LABEL_EVAL_MAX_ROWS = 200_000


# ------------------------------------------------------------------------------------------------ versions
def serving_version(model: MLModel, version: int | None = None) -> ModelVersion:
    """The version that answers predictions: the requested one, else production, else the newest staging one."""
    qs = ModelVersion.objects.filter(model=model).exclude(artifact_sha256="")
    if version is not None:
        try:
            wanted = int(version)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"version": ["Must be an integer."]}) from exc
        found = qs.filter(version=wanted).first()
        if found is None:
            raise ModelNotReady(f"Version {wanted} of this model does not exist.")
        return found
    found = qs.filter(stage="production").first() or qs.filter(stage="staging").order_by("-promoted_at",
                                                                                          "-version").first()
    if found is None:
        raise ModelNotReady()
    return found


def load_version_bundle(version: ModelVersion) -> ModelBundle:
    """sha256 + org-prefix verified, LRU cached (apps.ml.artifacts)."""
    return artifacts.load_bundle(version.artifact_key, version.artifact_sha256)


def _model(model_or_id) -> MLModel:
    if isinstance(model_or_id, MLModel):
        return model_or_id
    try:
        pk = model_or_id if isinstance(model_or_id, UUID) else UUID(str(model_or_id))
    except (TypeError, ValueError) as exc:
        raise NotFound() from exc
    obj = MLModel.objects.filter(id=pk).first()
    if obj is None:
        raise NotFound()
    return obj


# ------------------------------------------------------------------------------------------------ scoring
def _canonical_sha256(rows: Any) -> str:
    data = json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _frame_sha256(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(",".join(map(str, df.columns)).encode("utf-8"))
    h.update(pd.util.hash_pandas_object(df.astype(str), index=False).to_numpy().tobytes())
    return h.hexdigest()


def _display(value):
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _positive_index(bundle: ModelBundle) -> int | None:
    return 1 if bundle.task == "classification" and bundle.classes and len(bundle.classes) == 2 else None


def score(bundle: ModelBundle, X: pd.DataFrame, *, explain: bool = False) -> dict:
    """Raw scoring of a validated frame: predictions, probabilities, per-row drivers."""
    out = bundle.predict(X[bundle.feature_names])
    explanations = method = None
    if explain and bundle.task in ("classification", "regression"):
        explanations, method = explain_rows(bundle, X[bundle.feature_names], class_index=out["class_index"])
    elif explain:
        method = "not_supported"
    out["explanations"] = explanations
    out["explain_method"] = method
    return out


def _caller(request) -> dict:
    if request is None:
        kind, actor_id = current_actor.get() or ("system", None)
        return {"caller_type": "user" if kind == "user" else "system", "caller_id": actor_id, "api_key_prefix": ""}
    key_id = getattr(request, "_nexora_api_key_id", None)
    user_id = getattr(getattr(request, "user", None), "pk", None)
    if key_id is not None:
        prefix = getattr(getattr(request, "auth", None), "prefix", None)
        if not prefix:  # the auth record (ApiKeyRecord) carries no prefix: read it from the key row (tenant-scoped)
            from apps.accounts.models import ApiKey

            prefix = ApiKey.objects.filter(id=key_id).values_list("prefix", flat=True).first()
        return {"caller_type": "api_key", "caller_id": user_id, "api_key_prefix": str(prefix or "")[:32]}
    return {"caller_type": "user", "caller_id": user_id, "api_key_prefix": ""}


def _log_prediction(model: MLModel, version: ModelVersion, *, X: pd.DataFrame | None, predictions, n_rows: int,
                    source: str, latency_ms: float, input_sha256: str, explain: bool, request=None) -> None:
    counts: dict = {}
    if X is not None and version.reference_stats:
        try:
            counts = drift_engine.bin_counts(X, version.reference_stats, predictions=predictions)
        except Exception:  # noqa: BLE001 - monitoring must never break a prediction
            log.warning("ml: could not bin prediction inputs for drift (model %s)", model.id, exc_info=True)
    request_id = ""
    if request is not None:
        request_id = str(getattr(request, "request_id", "") or request.META.get("HTTP_X_REQUEST_ID", ""))[:64]
    PredictionLog.objects.create(model=model, model_version=version, n_rows=int(n_rows), source=source,
                                 latency_ms=round(float(latency_ms), 3), input_sha256=input_sha256,
                                 feature_counts=jsonable(counts), explain=bool(explain), request_id=request_id,
                                 **_caller(request))


def _input_error(exc: PredictionInputError) -> PredictionInputInvalid:
    extra = {"reason": exc.code, **exc.extra}
    if exc.errors:
        extra["errors"] = exc.errors  # RFC 7807 `errors` (field per missing column / bad value)
    return PredictionInputInvalid(exc.message, extra=extra)


def _forecast_rows(bundle: ModelBundle, rows, horizon) -> pd.DataFrame:
    fm = bundle.forecast
    if horizon is not None:
        try:
            steps = int(horizon)
        except (TypeError, ValueError) as exc:
            raise PredictionInputInvalid("`horizon` must be an integer.", extra={"reason": "invalid_horizon"}) \
                from exc
        if not 1 <= steps <= MAX_FORECAST_HORIZON:
            raise PredictionInputInvalid(f"`horizon` must be between 1 and {MAX_FORECAST_HORIZON}.",
                                         extra={"reason": "invalid_horizon"})
        return fm.forecast(steps)
    frame = validate_rows(rows, bundle.feature_schema)
    stamps = frame[bundle.time_column].tolist()
    try:
        return fm.forecast_at(stamps)
    except ValueError as exc:
        raise PredictionInputInvalid(str(exc), extra={"reason": "invalid_timestamp"}) from exc


def predict_rows(model: MLModel, rows, *, version: int | None = None, explain: bool = False, horizon=None,
                 source: str = "api", request=None) -> dict:
    """The JSON predict endpoint. Validates against the version's feature schema (missing columns -> 400)."""
    mv = serving_version(model, version)
    bundle = load_version_bundle(mv)
    started = time.perf_counter()
    base = {"model_id": str(model.id), "version": mv.version, "stage": mv.stage, "task_type": bundle.task}
    if bundle.task == "forecast":
        fc = _forecast_rows(bundle, rows, horizon)
        items = [{"period": r["period"], "prediction": _display(r["mean"]), "lower": _display(r.get("lower")),
                  "upper": _display(r.get("upper"))} for r in fc.to_dict(orient="records")]
        latency = (time.perf_counter() - started) * 1000
        _log_prediction(model, mv, X=None, predictions=None, n_rows=len(items), source=source, latency_ms=latency,
                        input_sha256=_canonical_sha256({"rows": rows, "horizon": horizon}), explain=False,
                        request=request)
        return {**base, "n_rows": len(items), "predictions": items, "latency_ms": round(latency, 2)}
    try:
        X = validate_rows(rows, bundle.feature_schema)
    except PredictionInputError as exc:
        raise _input_error(exc) from exc
    out = score(bundle, X, explain=explain)
    pred = out["prediction"]
    proba = out["proba"]
    classes = [str(c) for c in (bundle.classes or [])]
    items = []
    for i in range(len(X)):
        item: dict[str, Any] = {"prediction": _display(pred[i])}
        if proba is not None:
            item["probabilities"] = {c: round(float(p), 6) for c, p in zip(classes, proba[i], strict=False)}
            item["probability"] = round(float(proba[i].max()), 6)
        if out["explanations"] is not None:
            item["explanation"] = out["explanations"][i]
        items.append(item)
    latency = (time.perf_counter() - started) * 1000
    _log_prediction(model, mv, X=X, predictions=pred, n_rows=len(X), source=source, latency_ms=latency,
                    input_sha256=_canonical_sha256(rows), explain=explain, request=request)
    body = {**base, "n_rows": len(items), "predictions": items, "latency_ms": round(latency, 2)}
    if classes:
        body["classes"] = classes
    if explain:
        body["explain_method"] = out["explain_method"]
        if len(X) > MAX_EXPLAIN_ROWS:
            body["explained_rows"] = MAX_EXPLAIN_ROWS
    return body


def _probability_column(bundle: ModelBundle, proba: np.ndarray | None) -> np.ndarray | None:
    """Binary: probability of the positive (second) class; multiclass: probability of the predicted class."""
    if proba is None:
        return None
    pos = _positive_index(bundle)
    return proba[:, pos] if pos is not None else proba.max(axis=1)


def predict_frame(model_id, frame: pl.DataFrame, *, version: int | None = None, explain: bool = False,
                  source: str = "batch", request=None) -> pl.DataFrame:
    """CONTRACT: the input columns plus `prediction` and, for classifiers, `probability` (binary: probability of
    the positive class `classes[1]`; multiclass: of the predicted class), plus `explanation` (JSON text, the
    top drivers per row, first 1,000 rows) when `explain`. Raises ModelNotReady when no version can serve.

    Extra input columns pass through untouched; missing feature columns raise PredictionInputInvalid."""
    model = _model(model_id)
    mv = serving_version(model, version)
    bundle = load_version_bundle(mv)
    if bundle.task == "forecast":
        raise MLInvalid("Forecast models are scored by horizon (POST …/predict with `horizon`), not by rows.")
    if not isinstance(frame, pl.DataFrame):
        frame = pl.from_pandas(frame) if isinstance(frame, pd.DataFrame) else pl.DataFrame(frame)
    for reserved in (PREDICTION_COLUMN, PROBABILITY_COLUMN, EXPLANATION_COLUMN):
        if reserved in frame.columns:
            frame = frame.rename({reserved: f"input_{reserved}"})
    started = time.perf_counter()
    df = to_pandas(frame.select([c for c in bundle.feature_names if c in frame.columns]))
    try:
        validate_frame(df, bundle.feature_schema)
    except PredictionInputError as exc:
        raise _input_error(exc) from exc
    preds, probs, expl = [], [], []
    for start in range(0, max(len(df), 1), FRAME_CHUNK):
        chunk = df.iloc[start:start + FRAME_CHUNK]
        if not len(chunk):
            break
        out = score(bundle, chunk, explain=explain and start < MAX_EXPLAIN_ROWS)
        preds.append(np.asarray(out["prediction"]))
        p = _probability_column(bundle, out["proba"])
        if p is not None:
            probs.append(p)
        if explain:
            rows = out["explanations"] or [[] for _ in range(len(chunk))]
            expl.extend(json.dumps(r) if r else None for r in rows)
    pred = np.concatenate(preds) if preds else np.array([])
    columns: list[pl.Series] = [_prediction_series(bundle, pred)]
    if bundle.task == "classification":
        columns.append(pl.Series(PROBABILITY_COLUMN, np.concatenate(probs) if probs else [], dtype=pl.Float64))
    if explain:
        columns.append(pl.Series(EXPLANATION_COLUMN, expl, dtype=pl.Utf8))
    result = frame.with_columns(columns) if frame.height else frame.with_columns(
        [s.head(0) for s in columns])
    latency = (time.perf_counter() - started) * 1000
    _log_prediction(model, mv, X=df, predictions=pred, n_rows=len(df), source=source, latency_ms=latency,
                    input_sha256=_frame_sha256(df), explain=explain, request=request)
    return result


def _prediction_series(bundle: ModelBundle, pred: np.ndarray) -> pl.Series:
    if bundle.task == "regression":
        return pl.Series(PREDICTION_COLUMN, pred.astype(float), dtype=pl.Float64)
    if bundle.task == "clustering":
        return pl.Series(PREDICTION_COLUMN, pred.astype(int), dtype=pl.Int64)
    values = [_display(v) for v in pred.tolist()]
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values if v is not None):
        return pl.Series(PREDICTION_COLUMN, values, dtype=pl.Int64)
    return pl.Series(PREDICTION_COLUMN, [None if v is None else str(v) for v in values], dtype=pl.Utf8)


# ------------------------------------------------------------------------------------------ batch predictions
def _publish_frame():
    """apps.datasets.services.publish_frame (Data Engineering, W4), imported lazily."""
    from apps.datasets import services as ds

    fn = getattr(ds, "publish_frame", None)
    if fn is None:
        raise MLInvalid("Publishing prediction datasets is not available on this server yet.",
                        code="publish_unavailable")
    return fn


def start_batch_prediction(model: MLModel, *, user_id, data: dict, request=None):
    """Validate synchronously (version can serve, the caller may read every feature), then enqueue."""
    from apps.datasets.models import ATTESTED_CLASSIFICATIONS
    from apps.datasets.services import get_dataset

    mv = serving_version(model, data.get("version"))
    dataset = get_dataset(data["dataset_id"])
    usable, _excluded = allowed_columns(user_id, dataset.id)
    features = [c["name"] for c in mv.feature_schema]
    if mv.model.task_type == "forecast":
        raise MLInvalid("Forecast models do not score datasets; use the predict endpoint with `horizon`.")
    missing = [f for f in features if f not in usable]
    if missing:
        raise PolicyExcludedColumns(
            "The dataset is missing feature columns, or your data access policy does not allow them.",
            extra={"missing_or_excluded": missing})
    out_id = data.get("output_dataset_id")
    if out_id:
        get_dataset(out_id)  # 404 when not visible
    classification = dataset.classification
    if classification in ATTESTED_CLASSIFICATIONS and not data.get("attest"):
        classification = "internal"
    output_name = (data.get("output_name") or f"{model.name} predictions ({dataset.name})").strip()[:200]
    job = jobs.create_job("ml_batch_predict", user_id=user_id, message="Queued")
    audit.record("ml.batch_prediction.create", target=model, request=request, metadata={
        "version": mv.version, "dataset_id": str(dataset.id), "job_id": str(job.id), "output_name": output_name,
        "output_dataset_id": str(out_id) if out_id else None, "classification": classification})
    from .tasks import run_batch_prediction_task

    kwargs = {"org_id": str(require_org_id()), "model_id": str(model.id), "version": mv.version,
              "job_id": str(job.id), "dataset_id": str(dataset.id), "output_name": output_name,
              "output_dataset_id": str(out_id) if out_id else None, "explain": bool(data.get("explain")),
              "user_id": str(user_id) if user_id else None, "actor_id": str(user_id) if user_id else None,
              "classification": classification, "attest": bool(data.get("attest"))}
    transaction.on_commit(lambda: run_batch_prediction_task.delay(**kwargs))
    return job, mv


def run_batch_prediction(*, org_id, model_id, version, job_id, dataset_id, output_name, output_dataset_id=None,
                         explain=False, user_id=None, classification: str = "internal",
                         attest: bool = False) -> dict | None:
    org = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    try:
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=10, message="Loading the dataset")
            model = _model(model_id)
            mv = serving_version(model, version)
            gf = governed_frame(user_id, dataset_id, require=[c["name"] for c in mv.feature_schema])
            jobs.update(job_id, status="running", progress=35, message=f"Scoring {gf.frame.height} rows")
            scored = predict_frame(model, gf.frame, version=mv.version, explain=explain, source="batch")
            jobs.update(job_id, status="running", progress=75, message="Publishing the prediction dataset")
            dv = _publish_frame()(name=output_name, frame=scored, user_id=user_id, classification=classification,
                                  attest=attest, dataset_id=output_dataset_id,
                                  description=f"Predictions of model '{model.name}' v{mv.version} on "
                                              f"'{gf.dataset_name}' (version {gf.version_no}).",
                                  source="ml")
            out_dataset = str(getattr(dv, "dataset_id", "") or "")
            result = {"model_id": str(model.id), "version": mv.version, "rows": scored.height,
                      "source_dataset_id": str(gf.dataset_id), "source_dataset_version": gf.version_no,
                      "dataset_id": out_dataset, "dataset_version_id": str(getattr(dv, "id", "") or ""),
                      "dataset_version": getattr(dv, "version", None),
                      "publish_job_id": str(getattr(dv, "job_id", "") or "") or None,
                      "excluded_by_policy": gf.excluded_by_policy}
            _record_lineage(gf.version_id, mv.id, getattr(dv, "id", None))
            audit.record("ml.batch_prediction.finish", target=model, metadata=result)
            jobs.finish(job_id, result=result)
        _notify(user_id, "ml.batch_prediction_finished", f"Predictions ready: {model.name}",
                f"{scored.height} rows scored into '{output_name}'.",
                f"/datasets/{out_dataset}" if out_dataset else f"/ml/models/{model.id}", org=org)
        return result
    except Exception as exc:  # noqa: BLE001 - report the failure on the job
        log.exception("ml: batch prediction job %s failed", job_id)
        code = str(getattr(exc, "default_code", None) or getattr(exc, "code", None) or "ml_batch_failed")
        detail = getattr(exc, "detail", None)
        message = detail if isinstance(detail, str) else f"Batch prediction failed ({type(exc).__name__})."
        with tenant_step(org):
            jobs.fail(job_id, code=code[:60], message=str(message)[:500])
        _notify(user_id, "ml.batch_prediction_failed", "Batch prediction failed", str(message)[:300],
                f"/ml/models/{model_id}", org=org)
        return None


def _record_lineage(source_version_id, model_version_id, output_version_id) -> None:
    try:
        from apps.lineage.services import record as lineage_record

        with transaction.atomic():
            lineage_record("dataset_version", source_version_id, "ml_model_version", model_version_id,
                           label="scored by")
            if output_version_id:
                lineage_record("ml_model_version", model_version_id, "dataset_version", output_version_id,
                               label="batch predictions")
    except Exception:  # noqa: BLE001 - lineage is best effort
        log.warning("ml: lineage for batch prediction not recorded", exc_info=True)


def _notify(user_id, kind: str, title: str, body: str, link: str, *, org=None) -> None:
    """Best effort. Inside a tenant context it runs in a savepoint; with `org` it opens its own tenant step."""
    if not user_id:
        return
    try:
        from apps.core.notifications import notify

        if org is not None:
            with tenant_step(org):
                notify([user_id], kind, title[:200], body, link)
        else:
            with transaction.atomic():
                notify([user_id], kind, title[:200], body, link)
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.warning("ml: notification %s failed", kind, exc_info=True)


# ------------------------------------------------------------------------------------------------ monitoring
def _window_counts(version: ModelVersion, since, until=None) -> tuple[dict, int, int]:
    qs = PredictionLog.objects.filter(model_version=version, created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)
    logs = list(qs.values_list("feature_counts", "n_rows"))
    merged = drift_engine.merge_counts(c for c, _ in logs if c)
    return merged, len(logs), int(sum(n for _, n in logs))


def drift_for(version: ModelVersion, *, days: int = MONITORING_WINDOW_DAYS) -> dict:
    """Live PSI of the last `days` of prediction inputs against the version's training reference."""
    since = timezone.now() - timedelta(days=days)
    merged, n_calls, n_rows = _window_counts(version, since)
    report = drift_engine.drift_report(version.reference_stats or {}, merged)
    return {**report, "window_days": days, "n_predictions": n_calls, "n_rows": n_rows,
            "window_start": since.isoformat()}


def evaluate_labels(model: MLModel, version: ModelVersion, *, user_id, dataset_id=None) -> dict | None:
    """Performance on a labelled dataset that arrived after training (features + the target column)."""
    ds_id = dataset_id or model.label_dataset_id
    if not ds_id or not model.target or model.task_type not in ("classification", "regression"):
        return None
    features = [c["name"] for c in version.feature_schema]
    gf = governed_frame(user_id, ds_id, columns=[*features, model.target], require=[*features, model.target],
                        limit=LABEL_EVAL_MAX_ROWS)
    df = to_pandas(gf.frame)
    df = df[df[model.target].notna()]
    if not len(df):
        return {"dataset_id": str(gf.dataset_id), "rows": 0, "metrics": None,
                "message": "The label dataset has no rows with a target value."}
    bundle = load_version_bundle(version)
    out = bundle.predict(df[features])
    if model.task_type == "classification":
        index = {str(c): i for i, c in enumerate(bundle.classes or [])}
        y_raw = df[model.target].map(lambda v: str(int(v)) if isinstance(v, float) and float(v).is_integer()
                                     else str(v))
        known = y_raw.isin(list(index))
        y_true = y_raw[known].map(index).to_numpy(dtype=int)
        y_pred = np.asarray(out["class_index"])[known.to_numpy()]
        proba = out["proba"][known.to_numpy()] if out["proba"] is not None else None
        m = classification_metrics(y_true, y_pred, proba, list(bundle.classes or []))
        metrics = m["metrics"]
        extra = {"unseen_label_rows": int((~known).sum()), "confusion_matrix": m["confusion_matrix"]}
    else:
        y_true = pd.to_numeric(df[model.target], errors="coerce")
        ok = y_true.notna().to_numpy()
        metrics = regression_metrics(y_true.to_numpy()[ok], np.asarray(out["prediction"])[ok])["metrics"]
        extra = {}
    trained = (version.metrics or {}).get("test") or {}
    primary = (version.metrics or {}).get("primary_metric")
    return jsonable({"dataset_id": str(gf.dataset_id), "dataset_version": gf.version_no, "rows": int(len(df)),
                     "metrics": metrics, "training_metrics": {k: trained.get(k) for k in metrics},
                     "primary_metric": primary,
                     "primary_delta": (metrics.get(primary) - trained.get(primary))
                     if primary and metrics.get(primary) is not None and trained.get(primary) is not None else None,
                     **extra})


def _volume(model: MLModel, days: int = 30) -> list[dict]:
    since = timezone.now() - timedelta(days=days)
    rows = (PredictionLog.objects.filter(model=model, created_at__gte=since).annotate(day=TruncDay("created_at"))
            .values("day").annotate(calls=Count("id"), rows=Sum("n_rows")).order_by("day"))
    return [{"day": r["day"].date().isoformat(), "calls": r["calls"], "rows": int(r["rows"] or 0)} for r in rows]


def report_payload(r: DriftReport) -> dict:
    return {"id": str(r.id), "version": r.model_version.version if r.model_version_id else None,
            "window_start": r.window_start.isoformat(), "window_end": r.window_end.isoformat(),
            "n_predictions": r.n_predictions, "n_rows": r.n_rows, "status": r.status, "max_psi": r.max_psi,
            "prediction_psi": r.prediction_psi, "feature_psi": r.feature_psi, "alerts": r.alerts,
            "performance": r.performance, "trigger": r.trigger, "created_at": r.created_at.isoformat()}


def monitoring(model: MLModel) -> dict:
    """GET /ml/models/{id}/monitoring: live drift of the serving version, volume, latest reports, performance."""
    try:
        mv = serving_version(model)
    except ModelNotReady:
        mv = None
    since30 = timezone.now() - timedelta(days=30)
    agg = PredictionLog.objects.filter(model=model, created_at__gte=since30).aggregate(
        calls=Count("id"), rows=Sum("n_rows"), api=Count("id", filter=Q(source="api")),
        batch=Count("id", filter=Q(source="batch")))
    reports = list(DriftReport.objects.select_related("model_version").filter(model=model)[:20])
    latest_perf = next((r.performance for r in reports if r.performance), None)
    body = {
        "model_id": str(model.id), "task_type": model.task_type,
        "serving_version": mv.version if mv else None, "serving_stage": mv.stage if mv else None,
        "label_dataset_id": str(model.label_dataset_id) if model.label_dataset_id else None,
        "thresholds": {"warning_psi": drift_engine.WARN_PSI, "alert_psi": drift_engine.ALERT_PSI,
                       "min_rows": drift_engine.MIN_ROWS_FOR_DRIFT},
        "drift": drift_for(mv) if mv else None,
        "volume": {"last_30_days": {"calls": agg["calls"], "rows": int(agg["rows"] or 0), "api_calls": agg["api"],
                                    "batch_calls": agg["batch"]}, "daily": _volume(model)},
        "performance": latest_perf,
        "training_metrics": (mv.metrics or {}).get("test") if mv else None,
        "reports": [report_payload(r) for r in reports],
    }
    return jsonable(body)


def run_drift(model: MLModel, *, trigger: str = "scheduled", user_id=None, label_dataset_id=None,
              request=None) -> DriftReport | None:
    """Persist one DriftReport for the serving version over the window since its last report (max 7 days)."""
    try:
        mv = serving_version(model)
    except ModelNotReady:
        return None
    now = timezone.now()
    last = DriftReport.objects.filter(model=model, model_version=mv).order_by("-window_end").first()
    start = max(last.window_end, now - timedelta(days=MONITORING_WINDOW_DAYS)) if last and trigger == "scheduled" \
        else now - timedelta(days=MONITORING_WINDOW_DAYS)
    merged, n_calls, n_rows = _window_counts(mv, start, now)
    report = drift_engine.drift_report(mv.reference_stats or {}, merged)
    performance = None
    if label_dataset_id or model.label_dataset_id:
        try:
            performance = evaluate_labels(model, mv, user_id=user_id or model.created_by,
                                          dataset_id=label_dataset_id)
        except Exception as exc:  # noqa: BLE001 - a broken label dataset must not stop drift monitoring
            if trigger == "manual":
                raise
            performance = {"error": str(getattr(exc, "detail", "") or type(exc).__name__)}
    dr = DriftReport.objects.create(
        model=model, model_version=mv, window_start=start, window_end=now, n_predictions=n_calls, n_rows=n_rows,
        feature_psi=jsonable(report["features"]), prediction_psi=report["prediction_psi"],
        max_psi=report["max_psi"], status=report["status"], performance=performance,
        alerts=jsonable(report["alerts"]), trigger=trigger)
    audit.record("ml.model.drift", target=model, request=request,
                 metadata={"report_id": str(dr.id), "version": mv.version, "status": dr.status,
                           "max_psi": dr.max_psi, "trigger": trigger, "alerts": len(dr.alerts)})
    if dr.status == "alert":
        recipients = {model.created_by, mv.promoted_by} - {None}
        for uid in recipients:
            _notify(uid, "ml.drift_alert", f"Drift detected: {model.name}",
                    "; ".join(a["message"] for a in dr.alerts[:3]), f"/ml/models/{model.id}")
    return dr


def scan_orgs(using: str = "ops") -> list[UUID]:
    try:
        with connections[using].cursor() as cur:
            cur.execute(DRIFT_ORG_SCAN_SQL)
            return [UUID(str(r[0])) for r in cur.fetchall()]
    except DatabaseError:
        log.warning("ml: drift org scan failed", exc_info=True)
        return []


def run_drift_all(*_args, using: str = "ops", **_kwargs) -> dict:
    """The periodic job "ml.drift": one report per serving model and org. Never raises."""
    from apps.core.tenancy import tenant_context

    done = failed = 0
    for org_id in scan_orgs(using):
        try:
            with tenant_context(org_id):
                model_ids = list(ModelVersion.objects.filter(stage__in=("production", "staging"))
                                 .values_list("model_id", flat=True).distinct())
        except Exception:  # noqa: BLE001
            log.exception("ml: drift scan failed for org %s", org_id)
            failed += 1
            continue
        for model_id in model_ids:
            try:
                with tenant_context(org_id):
                    model = MLModel.objects.get(id=model_id)
                    if run_drift(model, trigger="scheduled") is not None:
                        done += 1
            except Exception:  # noqa: BLE001 - one model never stops the others
                log.exception("ml: drift report failed for model %s", model_id)
                failed += 1
    return {"reports": done, "failed": failed}


def enqueue_drift(*_args, **_kwargs) -> dict:
    """Periodic job fn: hand the scan to the task runner (queue "ml") and return at once."""
    from .tasks import drift_scan

    drift_scan.delay()
    return {"enqueued": True}


def register_periodic_job() -> bool:
    try:
        from apps.core.scheduler import register_periodic
    except ImportError:  # pragma: no cover - the scheduler exists from W3
        return False
    register_periodic(DRIFT_PERIODIC_NAME, DRIFT_EVERY_S, enqueue_drift)
    return True
