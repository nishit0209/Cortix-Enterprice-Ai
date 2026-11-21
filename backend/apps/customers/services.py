"""Public service API of apps.customers (Customer 360). Other apps import only from here.

Settings:   get_settings() / update_settings(data, request)          thresholds, weights, survivorship, country code
Sources:    create_source(data, user_id, request) / delete_source     dataset -> customer fields or events (policy
                                                                      checked: `query` ACL, no masked/denied columns)
Resolution: start_resolution(user_id, request) -> (run, job)          202; task body run_resolution(...)
Review:     decide_match(match, decision, user_id, request)           confirm merges, reject splits / blocks the pair
Profiles:   profile_payload / profile_detail / timeline_queryset      golden records, linked records, events
Segments:   create_segment / refresh_segment / refresh_all_segments   rule ({field, op, value}) and RFM quintiles
Scoring:    start_scoring(...)                                        409 ml_model_required until a model is ready
Features:   features_frame() / publish_features(user_id)              non-PII profile features -> publish_frame

All work runs inside the org's tenant context. Source data is read through apps.datasets.services.governed_frame
under the requesting user's policy, so denied/masked columns are never read and row policies apply.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import polars as pl
from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.exceptions import Conflict, NexoraError
from apps.core.tenancy import require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import matching as M
from .models import (
    CustomerEvent,
    CustomerProfile,
    CustomerProfileLink,
    CustomerScore,
    CustomerSegment,
    CustomerSegmentMember,
    CustomerSettings,
    CustomerSource,
    CustomerSourceRecord,
    IdentityMatch,
    ResolutionRun,
)

log = logging.getLogger(__name__)

PROFILE_FIELDS = ("external_id", "email", "phone", "name", "first_name", "last_name", "address", "dob", "created_at")
EVENT_FIELDS = ("customer_key", "occurred_at", "event_type", "event_type_column", "amount", "channel", "event_id",
                "attributes")
EVENT_COLUMN_FIELDS = ("customer_key", "occurred_at", "event_type_column", "amount", "channel", "event_id")
ORDER_EVENT_TYPES = ("order", "purchase", "transaction", "sale")
MAX_EVENT_ATTRIBUTES = 20
RUN_STALE_AFTER = dt.timedelta(hours=2)
BATCH = 2000

NOTIFY_FINISHED = "customers.resolution_finished"
NOTIFY_FAILED = "customers.resolution_failed"


class MlModelRequired(NexoraError):
    status_code = 409
    default_code = "ml_model_required"
    default_detail = ("Customer scoring needs a trained churn/CLV model with a production (or staging) version in the "
                      "ML workspace.")


class ColumnNotAllowed(NexoraError):
    status_code = 400
    default_code = "column_not_allowed"
    default_detail = "Masked or denied columns cannot be mapped."


class NoCustomerSources(Conflict):
    default_code = "customer_sources_required"
    default_detail = "Map at least one dataset to customer fields (POST /customers/sources) first."


# ----------------------------------------------------------------------------------------------- settings
def get_settings() -> CustomerSettings:
    obj = CustomerSettings.objects.first()
    if obj is None:
        obj, _ = CustomerSettings.objects.get_or_create(org_id=require_org_id())
    return obj


def settings_payload(s: CustomerSettings) -> dict:
    return {
        "auto_threshold": float(s.auto_threshold), "review_threshold": float(s.review_threshold),
        "weights": {**M.DEFAULT_WEIGHTS, **(s.weights or {})},
        "survivorship": {**M.DEFAULT_SURVIVORSHIP, **(s.survivorship or {})},
        "default_country_code": s.default_country_code,
        "features_dataset_id": str(s.features_dataset_id) if s.features_dataset_id else None,
        "defaults": {"auto_threshold": M.DEFAULT_AUTO, "review_threshold": M.DEFAULT_REVIEW,
                     "weights": M.DEFAULT_WEIGHTS, "survivorship": M.DEFAULT_SURVIVORSHIP,
                     "survivorship_rules": list(M.SURVIVORSHIP_RULES)},
    }


def update_settings(data: Mapping[str, Any], *, request=None) -> CustomerSettings:
    s = get_settings()
    before = settings_payload(s)
    auto = float(data.get("auto_threshold", s.auto_threshold))
    review = float(data.get("review_threshold", s.review_threshold))
    try:
        M.validate_thresholds(auto, review)
    except ValueError as exc:
        raise ValidationError({"auto_threshold": [str(exc)]}) from exc
    if "weights" in data:
        weights = data["weights"] or {}
        if not isinstance(weights, Mapping) or any(k not in M.DEFAULT_WEIGHTS for k in weights):
            raise ValidationError({"weights": [f"Keys must be among {', '.join(M.DEFAULT_WEIGHTS)}."]})
        try:
            clean = {k: float(v) for k, v in weights.items()}
        except (TypeError, ValueError) as exc:
            raise ValidationError({"weights": ["Weights must be numbers."]}) from exc
        if any(v < 0 or v > 10 for v in clean.values()) or sum({**M.DEFAULT_WEIGHTS, **clean}.values()) <= 0:
            raise ValidationError({"weights": ["Weights must be between 0 and 10 and not all zero."]})
        s.weights = clean
    if "survivorship" in data:
        rules = data["survivorship"] or {}
        if not isinstance(rules, Mapping) or any(k not in M.GOLDEN_FIELDS or v not in M.SURVIVORSHIP_RULES
                                                 for k, v in rules.items()):
            raise ValidationError({"survivorship": [
                f"Fields among {', '.join(M.GOLDEN_FIELDS)}; rules among {', '.join(M.SURVIVORSHIP_RULES)}."]})
        s.survivorship = dict(rules)
    if "default_country_code" in data:
        code = str(data["default_country_code"] or "").lstrip("+")
        if not code.isdigit() or not 1 <= len(code) <= 3:
            raise ValidationError({"default_country_code": ["A 1-3 digit calling code, e.g. 91."]})
        s.default_country_code = code
    s.auto_threshold, s.review_threshold = Decimal(str(round(auto, 4))), Decimal(str(round(review, 4)))
    s.save()
    audit.record("customers.settings.update", target=s, request=request,
                 changes={"before": before, "after": settings_payload(s)})
    return s


# ----------------------------------------------------------------------------------------------- sources
def _column_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def mapped_columns(source_kind: str, mapping: Mapping[str, Any]) -> list[str]:
    """Every dataset column a mapping reads (the policy check covers all of them)."""
    cols: list[str] = []
    if source_kind == "profile":
        for key in PROFILE_FIELDS:
            cols.extend(_column_list(mapping.get(key)))
    else:
        for key in EVENT_COLUMN_FIELDS:
            cols.extend(_column_list(mapping.get(key)))
        cols.extend(_column_list(mapping.get("attributes")))
    return list(dict.fromkeys(cols))


def validate_mapping(kind: str, mapping: Any) -> dict:
    """Shape checks of a field mapping (column existence and policy are checked against the dataset later)."""
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValidationError({"mapping": ["A non-empty object mapping fields to dataset columns is required."]})
    allowed = PROFILE_FIELDS if kind == "profile" else EVENT_FIELDS
    unknown = [k for k in mapping if k not in allowed]
    if unknown:
        raise ValidationError({"mapping": [f"Unknown field(s) {', '.join(map(str, unknown))}; allowed: "
                                           f"{', '.join(allowed)}."]})
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if value in (None, "", []):
            continue
        if key in ("address", "attributes"):
            cols = _column_list(value)
            if key == "attributes" and len(cols) > MAX_EVENT_ATTRIBUTES:
                raise ValidationError({"mapping": [f"At most {MAX_EVENT_ATTRIBUTES} attribute columns."]})
            out[key] = cols
        elif key == "event_type":
            out[key] = slugify(str(value)).replace("-", "_")[:40] or "event"
        elif not isinstance(value, str):
            raise ValidationError({"mapping": [f"{key} must name one column."]})
        else:
            out[key] = value
    if kind == "profile":
        if not any(out.get(k) for k in ("email", "phone", "name", "first_name", "last_name")):
            raise ValidationError({"mapping": ["Map at least one identifying field: email, phone or name."]})
    else:
        missing = [k for k in ("customer_key", "occurred_at") if not out.get(k)]
        if missing:
            raise ValidationError({"mapping": [f"Event sources need {', '.join(missing)}."]})
        out.setdefault("event_type", "event")
    return out


def create_source(data: Mapping[str, Any], *, user_id, request=None) -> CustomerSource:
    """POST /customers/sources. The user must hold `query` on the dataset; masked/denied columns cannot be mapped."""
    from apps.datasets import services as datasets

    kind = str(data.get("kind") or "profile")
    if kind not in ("profile", "event"):
        raise ValidationError({"kind": ["Must be profile or event."]})
    raw_mapping = data.get("mapping") if kind == "profile" else (data.get("mapping") or data.get("event_mapping"))
    mapping = validate_mapping(kind, raw_mapping or data.get("field_mapping"))
    try:
        dataset_id = UUID(str(data.get("dataset_id")))
    except ValueError as exc:
        raise ValidationError({"dataset_id": ["A dataset id is required."]}) from exc
    dataset, version, _policy, allowed, excluded = datasets.policy_columns(user_id, dataset_id, privilege="query")
    columns = mapped_columns(kind, mapping)
    all_columns = set(allowed) | set(excluded)
    unknown = [c for c in columns if c not in all_columns]
    if unknown:
        raise ValidationError({"mapping": [f"Unknown column(s): {', '.join(unknown)}."]})
    blocked = [c for c in columns if c in excluded]
    if blocked:
        raise ColumnNotAllowed(
            f"Column(s) {', '.join(blocked)} are masked or denied for you by the data policy and cannot be mapped.",
            extra={"excluded_by_policy": blocked})
    link = None
    if kind == "event":
        link_id = data.get("link_source_id")
        link = CustomerSource.objects.filter(id=link_id, kind="profile").first() if link_id else None
        if link is None:
            raise ValidationError({"link_source_id": [
                "Event sources reference a profile source whose external_id the customer_key column holds."]})
    name = str(data.get("name") or dataset.name).strip()[:200]
    system = slugify(str(data.get("source_system") or name))[:64] or "source"
    if CustomerSource.objects.filter(source_system=system).exists():
        raise Conflict(f"A customer source named {system!r} already exists.", code="customer_source_exists")
    try:
        priority = int(data.get("priority", 100))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"priority": ["An integer (lower = more trusted)."]}) from exc
    source = CustomerSource.objects.create(
        name=name, source_system=system, kind=kind, dataset_id=dataset.id,
        field_mapping=mapping if kind == "profile" else {}, event_mapping=mapping if kind == "event" else {},
        link_source=link, priority=max(0, min(priority, 10_000)), created_by=user_id,
        last_version_id=version.id,
    )
    audit.record("customers.source.create", target=source, request=request, metadata={
        "dataset_id": str(dataset.id), "kind": kind, "mapping": mapping, "source_system": system})
    return source


def delete_source(source: CustomerSource, *, request=None) -> None:
    if source.kind == "profile" and CustomerSource.objects.filter(link_source=source).exists():
        raise Conflict("Event sources still reference this source; delete them first.", code="customer_source_in_use")
    audit.record("customers.source.delete", target=source, request=request,
                 metadata={"source_system": source.source_system, "records": source.record_count})
    source.delete()


def source_payload(s: CustomerSource) -> dict:
    return {
        "id": str(s.id), "name": s.name, "source_system": s.source_system, "kind": s.kind,
        "dataset_id": str(s.dataset_id), "mapping": s.field_mapping if s.kind == "profile" else s.event_mapping,
        "link_source_id": str(s.link_source_id) if s.link_source_id else None, "priority": s.priority,
        "status": s.status, "last_error": s.last_error, "record_count": s.record_count,
        "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
        "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat(),
    }


# ----------------------------------------------------------------------------------------------- resolution
def start_resolution(*, user_id, request=None) -> tuple[ResolutionRun, Any]:
    """POST /customers/resolve -> 202. One run at a time per org (a run idle for 2 hours is considered lost)."""
    from .tasks import resolve_customers

    if not CustomerSource.objects.filter(kind="profile").exists():
        raise NoCustomerSources()
    active = ResolutionRun.objects.filter(status__in=("queued", "running"),
                                          updated_at__gte=timezone.now() - RUN_STALE_AFTER).first()
    if active is not None:
        raise Conflict("Identity resolution is already running.", code="resolution_running",
                       extra={"run_id": str(active.id), "job_id": str(active.job_id) if active.job_id else None})
    job = jobs.create_job("customers.resolve", user_id=user_id, message="Queued")
    run = ResolutionRun.objects.create(job_id=job.id, requested_by=user_id, status="queued")
    audit.record("customers.resolve.start", target=run, request=request, metadata={"job_id": str(job.id)})
    kwargs = {"org_id": str(require_org_id()), "run_id": str(run.id), "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: resolve_customers.delay(**kwargs))
    return run, job


def _progress(job_id, progress: int, message: str, **extra) -> None:
    jobs.update(job_id, status="running", progress=progress, message=message)
    jobs.publish(job_id, "log", {"message": message, **extra})


def run_resolution(*, org_id, run_id) -> dict | None:
    """Task body: sync sources -> block/score/cluster -> matches -> golden profiles -> events -> segments."""
    org = UUID(str(org_id))
    with tenant_step(org):
        run = ResolutionRun.objects.get(id=run_id)
        if run.status not in ("queued", "running"):
            return None
        run.status, run.started_at = "running", timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        job_id, user_id = run.job_id, run.requested_by
        _progress(job_id, 5, "Reading customer sources")
    try:
        stats: dict[str, Any] = {"sources": {}}
        with tenant_step(org):
            cfg = get_settings()
            profile_sources = list(CustomerSource.objects.filter(kind="profile").order_by("priority", "created_at"))
        for source in profile_sources:
            stats["sources"][source.source_system] = sync_profile_source(org, source, user_id, cfg.default_country_code)
        with tenant_step(org):
            _progress(job_id, 40, "Matching identities")
            outcome = rebuild_profiles(cfg)
            stats.update(outcome)
            _progress(job_id, 70, "Building customer timelines")
            event_sources = list(CustomerSource.objects.filter(kind="event").order_by("created_at"))
        for source in event_sources:
            stats["sources"][source.source_system] = sync_event_source(org, source, user_id)
        with tenant_step(org):
            relink_events()
            refresh_profile_metrics()
            _progress(job_id, 90, "Refreshing segments")
            stats["segments"] = refresh_all_segments()
            run = ResolutionRun.objects.get(id=run_id)
            run.status, run.finished_at, run.stats = "succeeded", timezone.now(), _jsonable(stats)
            run.save(update_fields=["status", "finished_at", "stats", "updated_at"])
            result = {"run_id": str(run.id), **run.stats}
            jobs.finish(job_id, result=result)
            audit.record("customers.resolve.finish", target=run, metadata=_summary(stats))
            _notify(user_id, ok=True, stats=stats)
        return result
    except Exception as exc:  # noqa: BLE001 - the run and job carry a stable code; details go to the log
        log.exception("customer resolution failed run=%s", run_id)
        code = getattr(exc, "default_code", None) if isinstance(exc, NexoraError) else "resolution_failed"
        message = str(getattr(exc, "detail", "")) if isinstance(exc, NexoraError) else "Identity resolution failed."
        with tenant_step(org):
            ResolutionRun.objects.filter(id=run_id).update(status="failed", finished_at=timezone.now(),
                                                           error={"code": code, "message": message})
            jobs.fail(job_id, code=code or "resolution_failed", message=message)
            audit.record("customers.resolve.failed", target_type="customers.resolutionrun", target_id=str(run_id),
                         metadata={"code": code})
            _notify(user_id, ok=False, message=message)
        return None


def _summary(stats: Mapping[str, Any]) -> dict:
    return {k: stats.get(k) for k in ("records", "profiles", "auto_merged", "pending_review", "entities")}


def _jsonable(value):
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID | dt.datetime | dt.date):
        return str(value)
    return value


def _notify(user_id, *, ok: bool, stats: Mapping | None = None, message: str = "") -> None:
    from apps.datasets.services import notify_users

    if ok:
        stats = stats or {}
        body = (f"{stats.get('profiles', 0):,} customer profiles from {stats.get('records', 0):,} records; "
                f"{stats.get('pending_review', 0):,} match(es) await review.")
        notify_users([user_id], NOTIFY_FINISHED, "Customer identity resolution finished", body,
                     "/customers/matches" if stats.get("pending_review") else "/customers")
    else:
        notify_users([user_id], NOTIFY_FAILED, "Customer identity resolution failed", message, "/customers")


def _source_error(org: UUID, source: CustomerSource, code: str, message: str, **extra) -> dict:
    with tenant_step(org):
        CustomerSource.objects.filter(id=source.id).update(
            status="error", last_error={"code": code, "message": message, **extra}, updated_at=timezone.now())
    return {"status": "error", "code": code, "message": message, **extra}


def _load_source_frame(org: UUID, source: CustomerSource, user_id, columns: list[str]):
    """governed_frame under the requester's policy; None + error dict when the policy no longer allows it."""
    from apps.datasets import services as datasets
    from apps.datasets.exceptions import DatasetNotReady

    try:
        with tenant_step(org):
            governed = datasets.governed_frame(user_id, source.dataset_id, columns)
    except NotFound:
        return None, _source_error(org, source, "dataset_not_accessible",
                                   "The dataset no longer exists or you may not query it.")
    except DatasetNotReady:
        return None, _source_error(org, source, "dataset_not_ready", "The dataset has no ready version.")
    except ValidationError:
        return None, _source_error(org, source, "excluded_by_policy",
                                   "Every mapped column is masked or denied for you.", excluded_by_policy=columns)
    if governed.excluded_by_policy:
        return None, _source_error(org, source, "excluded_by_policy",
                                   "Mapped columns are now masked or denied for you; update the mapping.",
                                   excluded_by_policy=list(governed.excluded_by_policy))
    return governed, None


def _row_value(row: Mapping[str, Any], cols: list[str]):
    if not cols:
        return None
    if len(cols) == 1:
        return row.get(cols[0])
    return [row.get(c) for c in cols]


def sync_profile_source(org: UUID, source: CustomerSource, user_id, country: str) -> dict:
    """Upsert the source's records (mapped + normalized); records gone from the dataset are removed."""
    mapping = source.field_mapping
    columns = mapped_columns("profile", mapping)
    governed, error = _load_source_frame(org, source, user_id, columns)
    if governed is None:
        return error
    fields = {key: _column_list(mapping.get(key)) for key in PROFILE_FIELDS if mapping.get(key)}
    seen: dict[str, dict] = {}
    duplicates = 0
    for row in governed.frame.iter_rows(named=True):
        raw = {key: _row_value(row, cols) for key, cols in fields.items()}
        key = M.source_key(raw)
        if key in seen:
            duplicates += 1
            continue
        norm = M.normalize_record(raw, default_country=country)
        seen[key] = {"attributes": _jsonable(raw), "normalized": norm,
                     "recorded_at": M.parse_timestamp(raw.get("created_at"))}
    with tenant_step(org):
        existing = dict(CustomerSourceRecord.objects.filter(source_id=source.id).values_list("source_key", "id"))
        stale = [rid for k, rid in existing.items() if k not in seen]
        for i in range(0, len(stale), BATCH):
            CustomerSourceRecord.objects.filter(id__in=stale[i:i + BATCH]).delete()
        updates, creates = [], []
        for key, data in seen.items():
            rec = CustomerSourceRecord(
                org_id=org, source_id=source.id, source_system=source.source_system, source_key=key,
                dataset_id=source.dataset_id, attributes=data["attributes"], normalized=data["normalized"],
                recorded_at=data["recorded_at"], ingested_at=timezone.now(),
            )
            if key in existing:
                rec.id = existing[key]
                updates.append(rec)
            else:
                creates.append(rec)
        CustomerSourceRecord.objects.bulk_create(creates, batch_size=BATCH)
        CustomerSourceRecord.objects.bulk_update(updates, ["attributes", "normalized", "recorded_at", "ingested_at",
                                                           "source_system", "dataset_id"], batch_size=BATCH)
        CustomerSource.objects.filter(id=source.id).update(
            status="active", last_error=None, last_synced_at=timezone.now(), last_version_id=governed.version.id,
            record_count=len(seen), updated_at=timezone.now())
    return {"status": "active", "records": len(seen), "created": len(creates), "updated": len(updates),
            "removed": len(stale), "duplicate_keys": duplicates, "rows_filtered_by_policy": governed.rows_filtered}


def _candidates(records: Iterable[CustomerSourceRecord], priorities: Mapping[UUID, int]) -> list[M.Candidate]:
    out = []
    for r in records:
        n = r.normalized or {}
        values = {"name": n.get("display_name"), "email": n.get("email"), "phone": n.get("phone"),
                  "address": n.get("address_display"), "dob": n.get("dob"), "created_at": n.get("created_at"),
                  "external_id": r.source_key if not r.source_key.startswith("row:") else None}
        out.append(M.Candidate(r.id, r.source_system, priorities.get(r.source_id, 100),
                               r.recorded_at.isoformat() if r.recorded_at else None, values))
    return out


def _apply_golden(profile: CustomerProfile, records: list[CustomerSourceRecord], priorities, rules) -> None:
    golden, trail = M.golden_record(_candidates(records, priorities), rules)
    profile.golden, profile.survivorship = _jsonable(golden), trail
    profile.record_count = len(records)
    profile.display_name = str(golden.get("name") or golden.get("email") or "")[:300]
    profile.email = str(golden.get("email") or "")[:320]
    profile.phone = str(golden.get("phone") or "")[:32]
    profile.sources = sorted({r.source_system for r in records})


def rebuild_profiles(cfg: CustomerSettings | None = None) -> dict:
    """Re-cluster every stored record (stored decisions honoured) and reconcile profiles (ids stay stable)."""
    from apps.quality.services import pair_key, reconcile_clusters

    cfg = cfg or get_settings()
    records = {r.id: r for r in CustomerSourceRecord.objects.all()}
    priorities = dict(CustomerSource.objects.values_list("id", "priority"))
    existing = {pair_key(a, b): (mid, status) for mid, a, b, status in
                IdentityMatch.objects.values_list("id", "record_a_id", "record_b_id", "status")}
    confirmed = [p for p, (_m, s) in existing.items() if s == "confirmed"]
    rejected = [p for p, (_m, s) in existing.items() if s == "rejected"]
    result = M.resolve({rid: r.normalized for rid, r in records.items()}, weights=cfg.weights,
                       auto=float(cfg.auto_threshold), review=float(cfg.review_threshold),
                       confirmed=confirmed, rejected=rejected)
    # matches: keep reviewer decisions, refresh machine ones, drop machine ones that no longer apply
    keep_ids = set()
    creates, updates = [], []
    for pair, (score, status) in result.matches.items():
        features = {**score.features, "rules": list(score.rules)}
        current = existing.get(pair)
        if current is None:
            creates.append(IdentityMatch(org_id=require_org_id(), record_a_id=pair[0], record_b_id=pair[1],
                                         score=Decimal(str(score.score)), features=features, status=status))
        else:
            keep_ids.add(current[0])
            if current[1] in ("auto_merged", "pending_review"):
                updates.append(IdentityMatch(id=current[0], score=Decimal(str(score.score)), features=features,
                                             status=status))
    stale = [mid for pair, (mid, status) in existing.items()
             if status in ("auto_merged", "pending_review") and mid not in keep_ids]
    for i in range(0, len(stale), BATCH):
        IdentityMatch.objects.filter(id__in=stale[i:i + BATCH]).delete()
    IdentityMatch.objects.bulk_create(creates, batch_size=BATCH)
    IdentityMatch.objects.bulk_update(updates, ["score", "features", "status"], batch_size=BATCH)
    # clusters -> profiles
    previous = dict(CustomerProfileLink.objects.values_list("record_id", "profile_id"))
    assignments = reconcile_clusters(result.clusters, previous)
    profiles = CustomerProfile.objects.in_bulk([pid for pid, _c in assignments if pid])
    used, new_profiles, changed = set(), [], []
    rules = cfg.survivorship or {}
    plan: list[tuple[CustomerProfile, list]] = []
    for pid, cluster in assignments:
        profile = profiles.get(pid) if pid else None
        if profile is None:
            profile = CustomerProfile(org_id=require_org_id())
            new_profiles.append(profile)
        else:
            used.add(profile.id)
            changed.append(profile)
        _apply_golden(profile, [records[r] for r in cluster], priorities, rules)
        plan.append((profile, cluster))
    gone = [pid for pid in CustomerProfile.objects.values_list("id", flat=True) if pid not in used]
    for i in range(0, len(gone), BATCH):
        CustomerProfile.objects.filter(id__in=gone[i:i + BATCH]).delete()
    CustomerProfile.objects.bulk_create(new_profiles, batch_size=BATCH)
    CustomerProfile.objects.bulk_update(changed, ["golden", "survivorship", "record_count", "display_name", "email",
                                                  "phone", "sources"], batch_size=BATCH)
    CustomerProfileLink.objects.all().delete()
    CustomerProfileLink.objects.bulk_create([
        CustomerProfileLink(org_id=require_org_id(), profile_id=profile.id, record_id=rid)
        for profile, cluster in plan for rid in cluster
    ], batch_size=BATCH)
    return {**result.stats, "profiles": len(plan), "profiles_created": len(new_profiles),
            "profiles_removed": len(gone)}


# ----------------------------------------------------------------------------------------------- events
def _parse_amount(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or abs(amount) >= Decimal("1e12"):
        return None
    return amount.quantize(Decimal("0.01"))


def sync_event_source(org: UUID, source: CustomerSource, user_id) -> dict:
    """Replace the source's events with the dataset's current rows (linked to records by customer_key)."""
    import hashlib

    mapping = source.event_mapping
    columns = mapped_columns("event", mapping)
    governed, error = _load_source_frame(org, source, user_id, columns)
    if governed is None:
        return error
    with tenant_step(org):
        keys = dict(CustomerSourceRecord.objects.filter(source_id=source.link_source_id)
                    .values_list("source_key", "id"))
    attr_cols = _column_list(mapping.get("attributes"))
    events, unmatched, invalid, seen = [], 0, 0, set()
    for idx, row in enumerate(governed.frame.iter_rows(named=True)):
        ckey = M._text(row.get(mapping["customer_key"]))
        when = M.parse_timestamp(row.get(mapping["occurred_at"]))
        if not ckey or when is None:
            invalid += 1
            continue
        if mapping.get("event_id"):
            ekey = M._text(row.get(mapping["event_id"]))[:255] or f"row:{idx}"
        else:
            ekey = "row:" + hashlib.sha1(repr(sorted((k, str(v)) for k, v in row.items())).encode(), usedforsecurity=False).hexdigest()[:32]
        if ekey in seen:
            continue
        seen.add(ekey)
        etype = mapping.get("event_type") or "event"
        if mapping.get("event_type_column"):
            etype = slugify(M._text(row.get(mapping["event_type_column"]))).replace("-", "_")[:40] or etype
        record_id = keys.get(ckey[:255])
        if record_id is None:
            unmatched += 1
        events.append(CustomerEvent(
            org_id=org, source_id=source.id, record_id=record_id, customer_key=ckey[:255], event_key=ekey,
            event_type=etype, channel=M._text(row.get(mapping["channel"]))[:60] if mapping.get("channel") else "",
            amount=_parse_amount(row.get(mapping["amount"])) if mapping.get("amount") else None,
            occurred_at=when, attributes=_jsonable({c: row.get(c) for c in attr_cols}),
        ))
    with tenant_step(org):
        CustomerEvent.objects.filter(source_id=source.id).delete()
        CustomerEvent.objects.bulk_create(events, batch_size=BATCH)
        CustomerSource.objects.filter(id=source.id).update(
            status="active", last_error=None, last_synced_at=timezone.now(), last_version_id=governed.version.id,
            record_count=len(events), updated_at=timezone.now())
    return {"status": "active", "events": len(events), "unmatched_customer_keys": unmatched,
            "invalid_rows": invalid, "rows_filtered_by_policy": governed.rows_filtered}


def relink_events() -> int:
    """Point every event at the profile its customer record belongs to (after clustering or a review decision)."""
    links = dict(CustomerProfileLink.objects.values_list("record_id", "profile_id"))
    changed = []
    for event in CustomerEvent.objects.only("id", "record_id", "profile_id").iterator(chunk_size=BATCH):
        target = links.get(event.record_id) if event.record_id else None
        if event.profile_id != target:
            event.profile_id = target
            changed.append(event)
    CustomerEvent.objects.bulk_update(changed, ["profile_id"], batch_size=BATCH)
    return len(changed)


def refresh_profile_metrics(profile_ids: Iterable[UUID] | None = None) -> None:
    qs = CustomerProfile.objects.all()
    if profile_ids is not None:
        qs = qs.filter(id__in=list(profile_ids))
    agg = {
        row["profile_id"]: row for row in CustomerEvent.objects.filter(profile__in=qs).values("profile_id").annotate(
            n=Count("id"), first=Min("occurred_at"), last=Max("occurred_at"),
            orders=Count("id", filter=Q(event_type__in=ORDER_EVENT_TYPES)),
            money=Sum("amount", filter=Q(event_type__in=ORDER_EVENT_TYPES)),
            last_order=Max("occurred_at", filter=Q(event_type__in=ORDER_EVENT_TYPES)),
        )
    }
    changed = []
    for p in qs.only("id"):
        row = agg.get(p.id) or {}
        p.event_count, p.order_count = int(row.get("n") or 0), int(row.get("orders") or 0)
        p.monetary = row.get("money") or Decimal("0")
        p.first_seen, p.last_seen, p.last_order_at = row.get("first"), row.get("last"), row.get("last_order")
        changed.append(p)
    CustomerProfile.objects.bulk_update(changed, ["event_count", "order_count", "monetary", "first_seen",
                                                  "last_seen", "last_order_at"], batch_size=BATCH)


# ----------------------------------------------------------------------------------------------- review
def match_payload(m: IdentityMatch, records: Mapping[UUID, CustomerSourceRecord] | None = None,
                  links: Mapping[UUID, UUID] | None = None) -> dict:
    def rec(rid):
        r = (records or {}).get(rid)
        if r is None:
            return {"id": str(rid)}
        return {"id": str(r.id), "source_system": r.source_system, "source_key": r.source_key,
                "attributes": r.attributes, "profile_id": str((links or {}).get(r.id)) if (links or {}).get(r.id)
                else None}

    return {"id": str(m.id), "score": float(m.score), "status": m.status, "features": m.features,
            "record_a": rec(m.record_a_id), "record_b": rec(m.record_b_id),
            "reviewed_by": str(m.reviewed_by) if m.reviewed_by else None,
            "decided_at": m.decided_at.isoformat() if m.decided_at else None, "note": m.note,
            "created_at": m.created_at.isoformat()}


def matches_payload(matches: list[IdentityMatch]) -> list[dict]:
    ids = {m.record_a_id for m in matches} | {m.record_b_id for m in matches}
    records = CustomerSourceRecord.objects.in_bulk(list(ids))
    links = dict(CustomerProfileLink.objects.filter(record_id__in=list(ids)).values_list("record_id", "profile_id"))
    return [match_payload(m, records, links) for m in matches]


def decide_match(match: IdentityMatch, decision: str, *, user_id, request=None, note: str = "") -> IdentityMatch:
    """confirm: the pair is one customer (profiles merged); reject: never the same customer (profile split)."""
    if decision not in ("confirm", "reject"):
        raise ValidationError({"decision": ["Must be confirm or reject."]})
    with transaction.atomic():
        match = IdentityMatch.objects.select_for_update().get(id=match.id)
        before = match.status
        match.status = "confirmed" if decision == "confirm" else "rejected"
        match.reviewed_by, match.decided_at, match.note = user_id, timezone.now(), str(note or "")[:500]
        match.save(update_fields=["status", "reviewed_by", "decided_at", "note", "updated_at"])
        links = dict(CustomerProfileLink.objects.filter(record_id__in=[match.record_a_id, match.record_b_id])
                     .values_list("record_id", "profile_id"))
        pa, pb = links.get(match.record_a_id), links.get(match.record_b_id)
        effect: dict[str, Any] = {}
        if decision == "confirm" and pa and pb and pa != pb:
            effect = merge_profiles(pa, pb)
        elif decision == "reject" and pa and pb and pa == pb:
            effect = split_profile(pa)
        audit.record(f"customers.match.{match.status}", target=match, request=request,
                     changes={"before": {"status": before}, "after": {"status": match.status}},
                     metadata={"score": float(match.score), **_jsonable(effect)})
    return match


def _golden_inputs():
    cfg = get_settings()
    return dict(CustomerSource.objects.values_list("id", "priority")), cfg.survivorship or {}


def merge_profiles(keep_id: UUID, other_id: UUID) -> dict:
    """Move every record of `other` to `keep` (the older profile survives), rebuild its golden record."""
    keep, other = CustomerProfile.objects.get(id=keep_id), CustomerProfile.objects.get(id=other_id)
    if str(other.id) < str(keep.id):
        keep, other = other, keep
    CustomerProfileLink.objects.filter(profile_id=other.id).update(profile_id=keep.id)
    CustomerEvent.objects.filter(profile_id=other.id).update(profile_id=keep.id)
    member_segments = set(CustomerSegmentMember.objects.filter(profile_id=keep.id).values_list("segment_id", flat=True))
    CustomerSegmentMember.objects.filter(profile_id=other.id).exclude(segment_id__in=member_segments).update(
        profile_id=keep.id)
    CustomerScore.objects.filter(profile_id=other.id).delete()  # scores are recomputed for the merged customer
    removed = other.id
    other.delete()
    _rebuild_golden([keep.id])
    refresh_profile_metrics([keep.id])
    return {"merged_into": keep.id, "removed_profile": removed}


def split_profile(profile_id: UUID) -> dict:
    """Re-cluster one profile's records with the stored machine matches minus rejected pairs."""
    from apps.quality.services import UnionFind, pair_key

    rec_ids = list(CustomerProfileLink.objects.filter(profile_id=profile_id).values_list("record_id", flat=True))
    uf = UnionFind(rec_ids)
    pairs = IdentityMatch.objects.filter(record_a_id__in=rec_ids, record_b_id__in=rec_ids)
    for a, b in pairs.filter(status="rejected").values_list("record_a_id", "record_b_id"):
        uf.forbid(a, b)
    ordered = pairs.filter(status__in=("confirmed", "auto_merged")).order_by("-status", "-score")
    for a, b in ordered.values_list("record_a_id", "record_b_id"):
        uf.union(*pair_key(a, b))
    groups = uf.groups()
    created = []
    for group in groups[1:]:  # the largest group keeps the profile id
        profile = CustomerProfile.objects.create(org_id=require_org_id())
        CustomerProfileLink.objects.filter(record_id__in=group).update(profile_id=profile.id)
        created.append(profile.id)
    _rebuild_golden([profile_id, *created])
    relink_events()
    refresh_profile_metrics([profile_id, *created])
    return {"split_profile": profile_id, "new_profiles": created}


def _rebuild_golden(profile_ids: list[UUID]) -> None:
    priorities, rules = _golden_inputs()
    by_profile: dict[UUID, list] = defaultdict(list)
    links = CustomerProfileLink.objects.filter(profile_id__in=profile_ids).values_list("profile_id", "record_id")
    records = CustomerSourceRecord.objects.in_bulk([r for _p, r in links])
    for pid, rid in links:
        by_profile[pid].append(records[rid])
    for profile in CustomerProfile.objects.filter(id__in=profile_ids):
        _apply_golden(profile, by_profile.get(profile.id, []), priorities, rules)
        profile.save()


# ----------------------------------------------------------------------------------------------- profiles
def profile_payload(p: CustomerProfile) -> dict:
    return {
        "id": str(p.id), "display_name": p.display_name, "email": p.email or None, "phone": p.phone or None,
        "golden": p.golden, "record_count": p.record_count, "sources": p.sources, "event_count": p.event_count,
        "order_count": p.order_count, "monetary": float(p.monetary),
        "first_seen": p.first_seen.isoformat() if p.first_seen else None,
        "last_seen": p.last_seen.isoformat() if p.last_seen else None,
        "last_order_at": p.last_order_at.isoformat() if p.last_order_at else None,
        "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat(),
    }


def profile_detail(p: CustomerProfile) -> dict:
    rec_ids = list(p.links.values_list("record_id", flat=True))
    records = CustomerSourceRecord.objects.filter(id__in=rec_ids).order_by("source_system", "source_key")
    segments = CustomerSegment.objects.filter(members__profile_id=p.id).order_by("name")
    matches = IdentityMatch.objects.filter(Q(record_a_id__in=rec_ids) | Q(record_b_id__in=rec_ids)).order_by("-score")
    return {
        **profile_payload(p), "survivorship": p.survivorship,
        "records": [{"id": str(r.id), "source_system": r.source_system, "source_key": r.source_key,
                     "attributes": r.attributes, "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None}
                    for r in records],
        "segments": [{"id": str(s.id), "name": s.name, "method": s.method} for s in segments],
        "matches": [{"id": str(m.id), "score": float(m.score), "status": m.status} for m in matches[:50]],
        "scores": [score_payload(s) for s in latest_scores(p)],
    }


def event_payload(e: CustomerEvent) -> dict:
    return {"id": str(e.id), "event_type": e.event_type, "occurred_at": e.occurred_at.isoformat(),
            "channel": e.channel or None, "amount": float(e.amount) if e.amount is not None else None,
            "source_system": e.source.source_system if e.source_id else None, "event_key": e.event_key,
            "attributes": e.attributes}


def score_payload(s: CustomerScore) -> dict:
    return {"id": str(s.id), "score_type": s.score_type, "value": s.value,
            "confidence": float(s.confidence) if s.confidence is not None else None, "explanation": s.explanation,
            "model_id": str(s.model_id) if s.model_id else None,
            "model_version_id": str(s.model_version_id) if s.model_version_id else None,
            "scored_at": s.scored_at.isoformat()}


def latest_scores(p: CustomerProfile) -> list[CustomerScore]:
    seen, out = set(), []
    for s in CustomerScore.objects.filter(profile_id=p.id).order_by("score_type", "-scored_at"):
        if s.score_type not in seen:
            seen.add(s.score_type)
            out.append(s)
    return out


# ----------------------------------------------------------------------------------------------- segments
def _profile_facts(rfm: Mapping[UUID, M.RfmRow], as_of: dt.datetime) -> list[dict]:
    facts = []
    for p in CustomerProfile.objects.all().iterator(chunk_size=BATCH):
        r = rfm.get(p.id)
        facts.append({
            "id": p.id, "golden": p.golden or {}, "record_count": p.record_count, "event_count": p.event_count,
            "order_count": p.order_count, "monetary": float(p.monetary), "sources": p.sources or [],
            "first_seen": p.first_seen, "last_seen": p.last_seen,
            "recency_days": (as_of - p.last_order_at).days if p.last_order_at else None,
            "rfm_label": r.label if r else None, "r": r.r if r else None, "f": r.f if r else None,
            "m": r.m if r else None,
        })
    return facts


def rfm_as_of() -> dt.datetime:
    """RFM reference time: the newest order event (so historical/synthetic data scores sensibly), else now."""
    latest = CustomerEvent.objects.filter(event_type__in=ORDER_EVENT_TYPES).aggregate(m=Max("occurred_at"))["m"]
    return latest or timezone.now()


def compute_rfm_rows() -> tuple[list[M.RfmRow], dt.datetime]:
    as_of = rfm_as_of()
    stats = CustomerProfile.objects.filter(order_count__gt=0, last_order_at__isnull=False).values_list(
        "id", "last_order_at", "order_count", "monetary")
    return M.compute_rfm([(pid, last, n, float(m)) for pid, last, n, m in stats], as_of), as_of


def create_segment(data: Mapping[str, Any], *, user_id, request=None) -> list[CustomerSegment]:
    """{name, method: rule, definition:{match, conditions}} -> [segment]; {method: rfm} -> one segment per RFM label."""
    method = str(data.get("method") or "rule")
    if method == "rfm":
        segments = refresh_rfm_segments(user_id=user_id)
        audit.record("customers.segment.rfm", target_type="customers.customersegment", target_id="rfm",
                     request=request, metadata={"segments": [s.name for s in segments]})
        return segments
    if method != "rule":
        raise ValidationError({"method": ["Must be rule or rfm (kmeans/hierarchical arrive with the ML workspace)."]})
    name = str(data.get("name") or "").strip()[:200]
    if not name:
        raise ValidationError({"name": ["A name is required."]})
    try:
        definition = M.validate_conditions(data.get("definition") or {})
    except ValueError as exc:
        raise ValidationError({"definition": [str(exc)]}) from exc
    if CustomerSegment.objects.filter(name=name).exists():
        raise Conflict(f"A segment named {name!r} already exists.", code="segment_exists")
    segment = CustomerSegment.objects.create(name=name, description=str(data.get("description") or "")[:2000],
                                             method="rule", definition=definition, created_by=user_id)
    refresh_segment(segment)
    audit.record("customers.segment.create", target=segment, request=request,
                 metadata={"definition": definition, "size": segment.size})
    return [segment]


def _replace_members(segment: CustomerSegment, members: Mapping[UUID, dict]) -> None:
    CustomerSegmentMember.objects.filter(segment_id=segment.id).delete()
    CustomerSegmentMember.objects.bulk_create([
        CustomerSegmentMember(org_id=require_org_id(), segment_id=segment.id, profile_id=pid, attributes=attrs)
        for pid, attrs in members.items()
    ], batch_size=BATCH)
    segment.size, segment.refreshed_at = len(members), timezone.now()
    segment.save(update_fields=["size", "refreshed_at", "updated_at"])


def refresh_segment(segment: CustomerSegment, *, facts: list[dict] | None = None) -> CustomerSegment:
    if segment.method == "rfm":
        refresh_rfm_segments()
        segment.refresh_from_db()
        return segment
    if segment.method != "rule":
        return segment
    if facts is None:
        rows, as_of = compute_rfm_rows()
        facts = _profile_facts({r.profile_id: r for r in rows}, as_of)
    members = {f["id"]: {} for f in facts if M.matches_definition(f, segment.definition)}
    _replace_members(segment, members)
    return segment


def refresh_rfm_segments(*, user_id=None) -> list[CustomerSegment]:
    rows, as_of = compute_rfm_rows()
    by_label: dict[str, dict] = {label: {} for label in M.RFM_LABELS}
    for r in rows:
        by_label[r.label][r.profile_id] = {"recency_days": r.recency_days, "frequency": r.frequency,
                                           "monetary": r.monetary, "r": r.r, "f": r.f, "m": r.m, "rfm": r.code}
    out = []
    for label, members in by_label.items():
        segment, _ = CustomerSegment.objects.get_or_create(
            name=f"RFM: {label}", defaults={"method": "rfm", "created_by": user_id,
                                            "description": f"RFM quintile segment '{label}'."})
        segment.method = "rfm"
        segment.definition = {"label": label, "as_of": as_of.isoformat(), "scoring": "quintiles (5 = best)"}
        segment.save(update_fields=["method", "definition", "updated_at"])
        _replace_members(segment, members)
        out.append(segment)
    return out


def refresh_all_segments() -> dict:
    segments = list(CustomerSegment.objects.all())
    if not segments:
        return {"refreshed": 0}
    rfm_rows, as_of = compute_rfm_rows()
    if any(s.method == "rfm" for s in segments):
        refresh_rfm_segments()
    facts = _profile_facts({r.profile_id: r for r in rfm_rows}, as_of)
    for s in segments:
        if s.method == "rule":
            refresh_segment(s, facts=facts)
    return {"refreshed": len(segments)}


def segment_payload(s: CustomerSegment) -> dict:
    return {"id": str(s.id), "name": s.name, "description": s.description, "method": s.method,
            "definition": s.definition, "size": s.size,
            "refreshed_at": s.refreshed_at.isoformat() if s.refreshed_at else None,
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}


# ----------------------------------------------------------------------------------------------- features / scoring
FEATURE_COLUMNS = ("profile_id", "record_count", "source_count", "event_count", "order_count", "monetary",
                   "avg_order_value", "recency_days", "tenure_days", "rfm_r", "rfm_f", "rfm_m")


def features_frame() -> pl.DataFrame:
    """One row per profile, numeric features only (no PII): input for churn/CLV models and segmentation."""
    rows, as_of = compute_rfm_rows()
    rfm = {r.profile_id: r for r in rows}
    data: dict[str, list] = {c: [] for c in FEATURE_COLUMNS}
    for p in CustomerProfile.objects.all().order_by("id").iterator(chunk_size=BATCH):
        r = rfm.get(p.id)
        data["profile_id"].append(str(p.id))
        data["record_count"].append(p.record_count)
        data["source_count"].append(len(p.sources or []))
        data["event_count"].append(p.event_count)
        data["order_count"].append(p.order_count)
        data["monetary"].append(float(p.monetary))
        data["avg_order_value"].append(round(float(p.monetary) / p.order_count, 2) if p.order_count else None)
        data["recency_days"].append((as_of - p.last_order_at).days if p.last_order_at else None)
        data["tenure_days"].append((as_of - p.first_seen).days if p.first_seen else None)
        data["rfm_r"].append(r.r if r else None)
        data["rfm_f"].append(r.f if r else None)
        data["rfm_m"].append(r.m if r else None)
    schema = {"profile_id": pl.String, "monetary": pl.Float64, "avg_order_value": pl.Float64,
              **{c: pl.Int64 for c in FEATURE_COLUMNS if c not in ("profile_id", "monetary", "avg_order_value")}}
    return pl.DataFrame(data, schema=schema)


def publish_features(*, user_id, request=None):
    """Publish features_frame() as the "Customer 360 features" dataset (a new version on later calls)."""
    from apps.datasets import services as datasets

    frame = features_frame()
    if frame.height == 0:
        raise Conflict("There are no customer profiles yet; run identity resolution first.", code="no_profiles")
    cfg = get_settings()
    target = None
    if cfg.features_dataset_id:
        try:
            target = datasets.get_dataset(cfg.features_dataset_id).id
        except NotFound:
            target = None
    version = datasets.publish_frame(
        name="Customer 360 features", frame=frame, user_id=user_id, classification="internal", dataset_id=target,
        description="Per-customer features from Customer 360 (record/event counts, RFM). No personal data.",
        source="customers")
    cfg.features_dataset_id = version.dataset_id
    cfg.save(update_fields=["features_dataset_id", "updated_at"])
    audit.record("customers.features.publish", target=cfg, request=request,
                 metadata={"dataset_id": str(version.dataset_id), "version_id": str(version.id),
                           "rows": frame.height})
    return version


def start_scoring(data: Mapping[str, Any], *, user_id, request=None):
    """POST /customers/scoring-runs. Needs a model in the ML registry with a production/staging version
    (apps.ml.services.predict_frame); until one exists this is 409 ml_model_required."""
    score_type = str(data.get("score_type") or "churn")
    if score_type not in ("churn", "clv"):
        raise ValidationError({"score_type": ["Must be churn or clv."]})
    model_id = data.get("model_id")
    try:
        from apps.ml import services as ml
    except ImportError as exc:
        raise MlModelRequired() from exc
    predict = getattr(ml, "predict_frame", None)
    not_ready = getattr(ml, "ModelNotReady", None)
    if predict is None or not model_id:
        raise MlModelRequired(extra={"score_type": score_type})
    frame = features_frame()
    if frame.height == 0:
        raise Conflict("There are no customer profiles yet; run identity resolution first.", code="no_profiles")
    try:
        predict(model_id, frame.head(1))
    except Exception as exc:
        if not_ready is not None and isinstance(exc, not_ready):
            raise MlModelRequired(extra={"score_type": score_type, "model_id": str(model_id)}) from exc
        raise
    from .tasks import score_customers

    job = jobs.create_job("customers.score", user_id=user_id, message="Queued")
    audit.record("customers.scoring.start", target_type="customers.customerscore", target_id=str(job.id),
                 request=request, metadata={"score_type": score_type, "model_id": str(model_id)})
    kwargs = {"org_id": str(require_org_id()), "job_id": str(job.id), "score_type": score_type,
              "model_id": str(model_id), "actor_id": str(user_id) if user_id else None}
    transaction.on_commit(lambda: score_customers.delay(**kwargs))
    return job


def run_scoring(*, org_id, job_id, score_type: str, model_id: str) -> dict | None:
    """Task body: predict_frame over every profile's features -> CustomerScore rows."""
    from apps.ml import services as ml

    org = UUID(str(org_id))
    try:
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=10, message="Scoring customers")
            frame = features_frame()
        scored = ml.predict_frame(model_id, frame, explain=True)
        now = timezone.now()
        rows = []
        for rec in scored.iter_rows(named=True):
            prob = rec.get("probability")
            value = {"prediction": _jsonable(rec.get("prediction"))}
            if prob is not None:
                value["probability"] = float(prob)
            rows.append(CustomerScore(
                org_id=org, profile_id=UUID(rec["profile_id"]), score_type=score_type, value=value,
                confidence=Decimal(str(round(float(prob), 4))) if prob is not None else None,
                explanation=_jsonable(rec.get("explanation") or {}), model_id=UUID(str(model_id)), scored_at=now))
        with tenant_step(org):
            CustomerScore.objects.bulk_create(rows, batch_size=BATCH)
            jobs.finish(job_id, result={"scored": len(rows), "score_type": score_type})
        return {"scored": len(rows)}
    except Exception as exc:  # noqa: BLE001
        log.exception("customer scoring failed job=%s", job_id)
        with tenant_step(org):
            jobs.fail(job_id, code=getattr(exc, "default_code", "scoring_failed"), message="Customer scoring failed.")
        return None
