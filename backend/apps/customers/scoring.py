"""Customer 360 features, ML scoring (churn / CLV) and next-best-action (API_SPECIFICATION Phase 6 `scoring-runs`).

Features (`feature_rows`): one row per resolved profile, numbers only (no PII), computed from the profile and its
timeline events as of a reference time:
    record_count, source_count, event_count, order_count, monetary, avg_order_value, recency_days, tenure_days,
    rfm_r, rfm_f, rfm_m, ticket_count, days_since_last_event
`features_frame()` is "now" (reference = the newest order event, so historical data scores sensibly).
`training_frame()` is point-in-time correct: features as of `cutoff = as_of - churn_window_days` from the events up to
the cutoff only, plus the labels observed after it: `churned` (no order in the window) and `future_value` (order
amount in the window). Train churn (classification on `churned`) and CLV (regression on `future_value`) models on
that published dataset in the ML workspace; the scoring run then predicts on `features_frame()`.

Scoring (`start_scoring`): churn/CLV through `apps.ml.services.predict_frame` with the model the caller names, or the
best compatible model of the org's customer project (task type, features present, production before staging). No
servable model -> 409 `ml_model_required`. Each run stores CustomerScore rows with the per-row explanation (top
drivers), then refreshes the next-best-action scores (rules from RFM segment, rule segments, churn and CLV).
`score_type: next_best_action` runs the rules alone (no model needed).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

import polars as pl
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.exceptions import Conflict, NexoraError
from apps.core.tenancy import require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import matching as M
from . import nba
from .models import (
    CustomerEvent,
    CustomerProfile,
    CustomerScore,
    CustomerScoringRun,
    CustomerSegmentMember,
)

log = logging.getLogger(__name__)

ORDER_EVENT_TYPES = ("order", "purchase", "transaction", "sale")
TICKET_EVENT_TYPES = ("ticket", "support_ticket", "support", "complaint", "case")
FEATURE_COLUMNS = ("profile_id", "record_count", "source_count", "event_count", "order_count", "monetary",
                   "avg_order_value", "recency_days", "tenure_days", "rfm_r", "rfm_f", "rfm_m", "ticket_count",
                   "days_since_last_event")
LABEL_COLUMNS = ("churned", "future_value")
FLOAT_COLUMNS = ("monetary", "avg_order_value", "future_value")
SCORE_TYPES = ("churn", "clv", "next_best_action")
SCORE_ALIASES = {"nba": "next_best_action", "next-best-action": "next_best_action", "ltv": "clv"}
TASK_FOR_SCORE = {"churn": "classification", "clv": "regression"}
TARGET_HINTS = {"churn": ("churn", "attrition", "lapsed", "retained"),
                "clv": ("clv", "ltv", "lifetime", "future_value", "value", "revenue", "spend")}
MIN_TRAINING_ROWS = 20
BATCH = 2000


class MlModelRequired(NexoraError):
    status_code = 409
    default_code = "ml_model_required"
    default_detail = ("Customer scoring needs a trained churn/CLV model with a production (or staging) version in the "
                      "ML workspace.")


class ModelIncompatible(NexoraError):
    status_code = 400
    default_code = "ml_model_incompatible"
    default_detail = "The model cannot score Customer 360 features."


# ----------------------------------------------------------------------------------------------- features
def rfm_as_of() -> dt.datetime:
    latest = CustomerEvent.objects.filter(event_type__in=ORDER_EVENT_TYPES).aggregate(m=Max("occurred_at"))["m"]
    return latest or timezone.now()


def _event_stats(*, until: dt.datetime | None = None, after: dt.datetime | None = None) -> dict[UUID, dict]:
    qs = CustomerEvent.objects.filter(profile_id__isnull=False)
    if until is not None:
        qs = qs.filter(occurred_at__lte=until)
    if after is not None:
        qs = qs.filter(occurred_at__gt=after)
    orders = Q(event_type__in=ORDER_EVENT_TYPES)
    rows = qs.values("profile_id").annotate(
        events=Count("id"), orders=Count("id", filter=orders), money=Sum("amount", filter=orders),
        last_order=Max("occurred_at", filter=orders), first_event=Min("occurred_at"), last_event=Max("occurred_at"),
        tickets=Count("id", filter=Q(event_type__in=TICKET_EVENT_TYPES)))
    return {r["profile_id"]: r for r in rows}


def feature_rows(*, reference: dt.datetime, cutoff: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Feature dicts per profile as of `reference`, from events up to `cutoff` (None = all events)."""
    stats = _event_stats(until=cutoff)
    rfm_input = [(pid, s["last_order"], s["orders"], float(s["money"] or 0)) for pid, s in stats.items()
                 if s["orders"] and s["last_order"] is not None]
    rfm = {r.profile_id: r for r in M.compute_rfm(rfm_input, reference)}
    out = []
    profiles = CustomerProfile.objects.all().order_by("id").values_list("id", "record_count", "sources", "first_seen")
    for pid, record_count, sources, first_seen in profiles.iterator(chunk_size=BATCH):
        s = stats.get(pid) or {}
        starts = [t for t in (first_seen, s.get("first_event")) if t is not None and (cutoff is None or t <= cutoff)]
        if cutoff is not None and not starts:
            continue  # the customer did not exist yet at the cutoff
        orders = int(s.get("orders") or 0)
        money = float(s.get("money") or 0)
        r = rfm.get(pid)
        out.append({
            "profile_id": str(pid), "record_count": int(record_count or 0), "source_count": len(sources or []),
            "event_count": int(s.get("events") or 0), "order_count": orders, "monetary": round(money, 2),
            "avg_order_value": round(money / orders, 2) if orders else None,
            "recency_days": max(0, (reference - s["last_order"]).days) if s.get("last_order") else None,
            "tenure_days": max(0, (reference - min(starts)).days) if starts else None,
            "rfm_r": r.r if r else None, "rfm_f": r.f if r else None, "rfm_m": r.m if r else None,
            "ticket_count": int(s.get("tickets") or 0),
            "days_since_last_event": max(0, (reference - s["last_event"]).days) if s.get("last_event") else None,
            "_rfm_label": r.label if r else None,
        })
    return out


def _frame(rows: list[dict], columns: tuple[str, ...]) -> pl.DataFrame:
    schema = {c: (pl.String if c == "profile_id" else pl.Float64 if c in FLOAT_COLUMNS else pl.Int64) for c in columns}
    return pl.DataFrame({c: [r.get(c) for r in rows] for c in columns}, schema=schema)


def features_frame() -> pl.DataFrame:
    """Current features (reference = newest order event): the scoring input and the "Customer 360 features" set."""
    return _frame(feature_rows(reference=rfm_as_of()), FEATURE_COLUMNS)


def training_frame(window_days: int) -> tuple[pl.DataFrame, dict]:
    """Point-in-time training set: features as of `cutoff`, labels from the following `window_days`."""
    as_of = rfm_as_of()
    cutoff = as_of - dt.timedelta(days=int(window_days))
    rows = [r for r in feature_rows(reference=cutoff, cutoff=cutoff) if r["order_count"] > 0]
    future = _event_stats(until=as_of, after=cutoff)
    for r in rows:
        f = future.get(UUID(r["profile_id"])) or {}
        r["churned"] = 0 if int(f.get("orders") or 0) > 0 else 1
        r["future_value"] = round(float(f.get("money") or 0), 2)
    meta = {"as_of": as_of.isoformat(), "cutoff": cutoff.isoformat(), "window_days": int(window_days),
            "rows": len(rows), "churned": sum(r["churned"] for r in rows)}
    return _frame(rows, FEATURE_COLUMNS + LABEL_COLUMNS), meta


# ----------------------------------------------------------------------------------------------- model selection
def _ml():
    try:
        from apps.ml import services as ml
    except ImportError:
        return None
    return ml if callable(getattr(ml, "predict_frame", None)) else None


def _servable(model) -> Any:
    from apps.ml.models import ModelVersion

    qs = ModelVersion.objects.filter(model=model).exclude(artifact_sha256="")
    return qs.filter(stage="production").first() or qs.filter(stage="staging").order_by("-promoted_at",
                                                                                      "-version").first()


def _required_features(version) -> list[str]:
    return [c["name"] for c in (version.feature_schema or []) if isinstance(c, dict) and c.get("required", True)]


def _missing(version) -> list[str]:
    return [c for c in _required_features(version) if c not in FEATURE_COLUMNS or c == "profile_id"]


def select_model(score_type: str, model_id=None, *, project_datasets: set[UUID] = frozenset()) -> tuple[Any, Any, str]:
    """-> (MLModel, ModelVersion, selection). Raises MlModelRequired / ModelIncompatible / NotFound."""
    from apps.ml.models import MLModel

    task = TASK_FOR_SCORE[score_type]
    if model_id:
        try:
            model = MLModel.objects.select_related("project").get(id=UUID(str(model_id)))
        except (MLModel.DoesNotExist, ValueError, TypeError) as exc:
            raise NotFound("Model not found.") from exc
        if model.task_type != task:
            raise ModelIncompatible(f"A {score_type} score needs a {task} model; this one is {model.task_type}.",
                                    extra={"score_type": score_type, "task_type": model.task_type})
        version = _servable(model)
        if version is None:
            raise MlModelRequired("The model has no production or staging version yet.",
                                  extra={"score_type": score_type, "model_id": str(model.id)})
        missing = _missing(version)
        if missing:
            raise ModelIncompatible("The model needs features that Customer 360 does not produce.",
                                    extra={"missing_features": missing, "available_features": list(FEATURE_COLUMNS[1:])})
        return model, version, "explicit"
    ranked = []
    hints = TARGET_HINTS[score_type]
    for model in MLModel.objects.select_related("project").filter(task_type=task):
        version = _servable(model)
        if version is None or _missing(version):
            continue
        target = (model.target or getattr(model.project, "target", "") or "").lower()
        ranked.append(((model.project.dataset_id in project_datasets, any(h in target for h in hints),
                        version.stage == "production", version.promoted_at or version.created_at), model, version))
    if not ranked:
        raise MlModelRequired(extra={"score_type": score_type,
                                     "hint": "Publish the Customer 360 training set (POST customers/features/publish "
                                             "{kind: training}), train a model on it and promote a version."})
    ranked.sort(key=lambda t: t[0], reverse=True)
    return ranked[0][1], ranked[0][2], "auto"


# ----------------------------------------------------------------------------------------------- runs
def normalize_score_type(value) -> str:
    raw = str(value or "churn").strip().lower()
    score_type = SCORE_ALIASES.get(raw, raw)
    if score_type not in SCORE_TYPES:
        raise ValidationError({"score_type": ["Must be churn, clv or next_best_action."]})
    return score_type


def run_payload(r: CustomerScoringRun) -> dict:
    return {"id": str(r.id), "score_type": r.score_type, "status": r.status,
            "job_id": str(r.job_id) if r.job_id else None,
            "model_id": str(r.model_id) if r.model_id else None, "model_name": r.model_name or None,
            "model_version_id": str(r.model_version_id) if r.model_version_id else None,
            "model_version": r.model_version, "model_stage": r.model_stage or None, "selection": r.selection,
            "requested_by": str(r.requested_by) if r.requested_by else None, "stats": r.stats, "error": r.error,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat()}


def start_scoring(data: Mapping[str, Any], *, user_id, project_datasets: set[UUID] = frozenset(), request=None):
    """POST /customers/scoring-runs {score_type: churn|clv|next_best_action, model_id?} -> (run, job), 202."""
    from .tasks import score_customers

    score_type = normalize_score_type(data.get("score_type"))
    if not CustomerProfile.objects.exists():
        raise Conflict("There are no customer profiles yet; run identity resolution first.", code="no_profiles")
    active = CustomerScoringRun.objects.filter(score_type=score_type, status__in=("queued", "running"),
                                               updated_at__gte=timezone.now() - dt.timedelta(hours=2)).first()
    if active is not None:
        raise Conflict("A scoring run of this type is already running.", code="scoring_running",
                       extra={"run_id": str(active.id), "job_id": str(active.job_id) if active.job_id else None})
    model = version = None
    selection = "rules"
    if score_type != "next_best_action":
        if _ml() is None:
            raise MlModelRequired(extra={"score_type": score_type})
        model, version, selection = select_model(score_type, data.get("model_id"), project_datasets=project_datasets)
    job = jobs.create_job("customers.score", user_id=user_id, message="Queued")
    run = CustomerScoringRun.objects.create(
        score_type=score_type, job_id=job.id, requested_by=user_id, selection=selection,
        model_id=model.id if model else None, model_name=model.name if model else "",
        model_version_id=version.id if version else None, model_version=version.version if version else None,
        model_stage=version.stage if version else "")
    audit.record("customers.scoring.start", target=run, request=request, metadata={
        "score_type": score_type, "model_id": str(model.id) if model else None,
        "model_version": version.version if version else None, "selection": selection})
    kwargs = {"org_id": str(require_org_id()), "run_id": str(run.id), "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: score_customers.delay(**kwargs))
    return run, job


def _parse_explanation(raw) -> list:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else [raw]


def _score_rows(org: UUID, run: CustomerScoringRun, scored: pl.DataFrame, window_days: int, now) -> list:
    rows = []
    for rec in scored.iter_rows(named=True):
        features = {c: rec.get(c) for c in FEATURE_COLUMNS[1:] if c in rec}
        explanation = {"drivers": _parse_explanation(rec.get("explanation")), "features": features,
                       "model": {"id": str(run.model_id), "name": run.model_name, "version": run.model_version,
                                 "stage": run.model_stage}}
        pred = rec.get("prediction")
        prob = rec.get("probability")
        confidence = None
        if run.score_type == "churn":
            p = float(prob) if prob is not None else (float(pred) if isinstance(pred, int | float) else None)
            value = {"probability": round(p, 4) if p is not None else None, "prediction": pred,
                     "risk_band": nba.churn_band(p)}
            if prob is not None:
                confidence = Decimal(str(round(max(float(prob), 1 - float(prob)), 4)))
        else:
            value = {"predicted_value": round(float(pred), 2) if pred is not None else None,
                     "horizon_days": window_days}
        rows.append(CustomerScore(org_id=org, profile_id=UUID(rec["profile_id"]), score_type=run.score_type,
                                  value=value, confidence=confidence, explanation=explanation, model_id=run.model_id,
                                  model_version_id=run.model_version_id, scored_at=now))
    return rows


def _latest_values(score_type: str) -> dict[UUID, dict]:
    qs = (CustomerScore.objects.filter(score_type=score_type).order_by("profile_id", "-scored_at")
          .distinct("profile_id").values_list("profile_id", "value"))
    return dict(qs)


def compute_nba(now) -> list[CustomerScore]:
    """Next-best-action for every profile from RFM, rule segments and the latest churn / CLV scores."""
    org = require_org_id()
    churn = _latest_values("churn")
    clv = {pid: float(v.get("predicted_value")) for pid, v in _latest_values("clv").items()
           if isinstance(v, dict) and v.get("predicted_value") is not None}
    clv_pct = nba.value_percentiles(clv)
    segments: dict[UUID, list[str]] = defaultdict(list)
    for pid, name in CustomerSegmentMember.objects.filter(segment__method="rule").values_list("profile_id",
                                                                                               "segment__name"):
        segments[pid].append(name)
    out = []
    for row in feature_rows(reference=rfm_as_of()):
        pid = UUID(row["profile_id"])
        cv = churn.get(pid) or {}
        decision = nba.decide(rfm_label=row["_rfm_label"], order_count=row["order_count"],
                              ticket_count=row["ticket_count"],
                              churn_probability=cv.get("probability") if isinstance(cv, dict) else None,
                              clv_percentile=clv_pct.get(pid), segments=sorted(segments.get(pid, [])))
        signals = decision.pop("signals")
        rule = decision.pop("rule")
        out.append(CustomerScore(org_id=org, profile_id=pid, score_type="next_best_action", value=decision,
                                 confidence=None, explanation={"rule": rule, "signals": signals, "method": "rules"},
                                 scored_at=now))
    return out


def run_scoring(*, org_id, run_id) -> dict | None:
    """Task body: predict (churn/CLV) -> CustomerScore rows, then refresh next-best-action."""
    org = UUID(str(org_id))
    with tenant_step(org):
        run = CustomerScoringRun.objects.filter(id=run_id).first()
        if run is None or run.status not in ("queued", "running"):
            return None
        run.status, run.started_at = "running", timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        jobs.update(run.job_id, status="running", progress=10, message="Computing customer features")
        from .services import get_settings

        window = int(get_settings().churn_window_days)
    try:
        stats: dict[str, Any] = {"score_type": run.score_type}
        if run.score_type != "next_best_action":
            with tenant_step(org):
                frame = features_frame()
            ml = _ml()
            if ml is None:
                raise MlModelRequired()
            with tenant_step(org):
                jobs.update(run.job_id, progress=40, message=f"Scoring {frame.height} customers")
                scored = ml.predict_frame(str(run.model_id), frame, version=run.model_version, explain=True)
                now = timezone.now()
                rows = _score_rows(org, run, scored, window, now)
                CustomerScore.objects.bulk_create(rows, batch_size=BATCH)
            stats["scored"] = len(rows)
            if run.score_type == "churn":
                bands = defaultdict(int)
                for r in rows:
                    bands[r.value.get("risk_band") or "unknown"] += 1
                stats["risk_bands"] = dict(bands)
            else:
                vals = [r.value["predicted_value"] for r in rows if r.value.get("predicted_value") is not None]
                stats["predicted_value_total"] = round(sum(vals), 2)
        with tenant_step(org):
            jobs.update(run.job_id, progress=80, message="Updating next best actions")
            nba_rows = compute_nba(timezone.now())
            CustomerScore.objects.bulk_create(nba_rows, batch_size=BATCH)
            actions = defaultdict(int)
            for r in nba_rows:
                actions[r.value["action"]] += 1
            stats["next_best_actions"] = dict(actions)
            if run.score_type == "next_best_action":
                stats["scored"] = len(nba_rows)
            run = CustomerScoringRun.objects.get(id=run_id)
            run.status, run.finished_at, run.stats = "succeeded", timezone.now(), stats
            run.save(update_fields=["status", "finished_at", "stats", "updated_at"])
            jobs.finish(run.job_id, result={"run_id": str(run.id), **stats})
            audit.record("customers.scoring.finish", target=run, metadata=stats)
        return stats
    except Exception as exc:  # noqa: BLE001 - the run and job carry a stable code; details go to the log
        log.exception("customer scoring failed run=%s", run_id)
        code = getattr(exc, "default_code", None) if isinstance(exc, NexoraError) else None
        message = str(getattr(exc, "detail", "")) if isinstance(exc, NexoraError) else "Customer scoring failed."
        with tenant_step(org):
            CustomerScoringRun.objects.filter(id=run_id).update(
                status="failed", finished_at=timezone.now(), updated_at=timezone.now(),
                error={"code": code or "scoring_failed", "message": message})
            run = CustomerScoringRun.objects.get(id=run_id)
            if run.job_id:
                jobs.fail(run.job_id, code=code or "scoring_failed", message=message)
        return None
