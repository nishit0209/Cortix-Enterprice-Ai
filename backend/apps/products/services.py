"""Public service API of apps.products (Product 360). Other apps import only from here.

Settings:    get_settings() / update_settings(data, request)          thresholds, weights, brand aliases
Sources:     create_source(data, user_id, request) / delete_source     dataset -> product fields (policy checked:
                                                                       `query` ACL, no masked/denied columns)
Normalize:   start_run("normalize", user_id) -> (run, job)             202; task body run_normalize(...)
             sync -> normalize (SKU, brand, units, attributes) -> match -> masters -> attributes -> quality
             -> embeddings (when the local model is available)
Review:      decide_match(match, decision, user_id)                    confirm merges, reject splits / blocks
Corrections: decide_correction(c, decision, user_id)                   applied ONLY on approve; audited
Search:      search(query, k)                                          semantic (local embeddings) or text fallback
Enrich:      start_run("enrich", user_id) -> (run, job)                202; local rule suggestions -> proposed
                                                                       corrections (never applied automatically)

Source data is read through apps.datasets.services.governed_frame under the requesting user's policy.
Enrichment is local (rules). A Gemini enrichment path needs a registered prompt (AI/ML-owned) and is not offered.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections import Counter, defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.exceptions import Conflict, NexoraError
from apps.core.tenancy import require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import matching as M
from .models import (
    ProductAttribute,
    ProductCorrection,
    ProductMaster,
    ProductMatch,
    ProductQualityScore,
    ProductRun,
    ProductSettings,
    ProductSource,
    ProductSourceRecord,
)

log = logging.getLogger(__name__)

FIELDS = M.MAPPING_FIELDS  # sku, name, brand, category, price, pack_size, unit, attributes, external_id
MAX_ATTRIBUTE_COLUMNS = 20
CORRECTABLE = ("name", "brand", "category", "size", "price", "sku")
RUN_STALE_AFTER = dt.timedelta(hours=2)
BATCH = 2000
SEARCH_MAX_K = 50

NOTIFY_FINISHED = "products.run_finished"
NOTIFY_FAILED = "products.run_failed"


class ColumnNotAllowed(NexoraError):
    status_code = 400
    default_code = "column_not_allowed"
    default_detail = "Masked or denied columns cannot be mapped."


class NoProductSources(Conflict):
    default_code = "product_sources_required"
    default_detail = "Map at least one dataset to product fields (POST /products/sources) first."


class NoProductMasters(Conflict):
    default_code = "product_masters_required"
    default_detail = "Run normalization (POST /products/normalize) first."


# ----------------------------------------------------------------------------------------------- settings
def get_settings() -> ProductSettings:
    obj = ProductSettings.objects.first()
    if obj is None:
        obj, _ = ProductSettings.objects.get_or_create(org_id=require_org_id())
    return obj


def settings_payload(s: ProductSettings) -> dict:
    return {"auto_threshold": float(s.auto_threshold), "review_threshold": float(s.review_threshold),
            "weights": {**M.DEFAULT_WEIGHTS, **(s.weights or {})}, "brand_aliases": s.brand_aliases or {},
            "defaults": {"auto_threshold": M.DEFAULT_AUTO, "review_threshold": M.DEFAULT_REVIEW,
                         "weights": M.DEFAULT_WEIGHTS}}


def update_settings(data: Mapping[str, Any], *, request=None) -> ProductSettings:
    s = get_settings()
    before = settings_payload(s)
    try:
        auto = float(data.get("auto_threshold", s.auto_threshold))
        review = float(data.get("review_threshold", s.review_threshold))
        M.validate_thresholds(auto, review)
    except (TypeError, ValueError) as exc:
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
    if "brand_aliases" in data:
        aliases = data["brand_aliases"] or {}
        if not isinstance(aliases, Mapping) or len(aliases) > 2000 or not all(
                isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in aliases.items()):
            raise ValidationError({"brand_aliases": ["An object {alias: canonical brand} of non-empty strings."]})
        s.brand_aliases = {k.strip()[:200]: v.strip()[:200] for k, v in aliases.items()}
    s.auto_threshold, s.review_threshold = Decimal(str(round(auto, 4))), Decimal(str(round(review, 4)))
    s.save()
    audit.record("products.settings.update", target=s, request=request,
                 changes={"before": before, "after": settings_payload(s)})
    return s


# ----------------------------------------------------------------------------------------------- sources
def _columns(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def validate_mapping(mapping: Any) -> dict:
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValidationError({"mapping": ["A non-empty object mapping fields to dataset columns is required."]})
    unknown = [k for k in mapping if k not in FIELDS]
    if unknown:
        raise ValidationError({"mapping": [f"Unknown field(s) {', '.join(map(str, unknown))}; allowed: "
                                           f"{', '.join(FIELDS)}."]})
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if value in (None, "", []):
            continue
        if key == "attributes":
            cols = _columns(value)
            if len(cols) > MAX_ATTRIBUTE_COLUMNS:
                raise ValidationError({"mapping": [f"At most {MAX_ATTRIBUTE_COLUMNS} attribute columns."]})
            out[key] = cols
        elif not isinstance(value, str):
            raise ValidationError({"mapping": [f"{key} must name one column."]})
        else:
            out[key] = value
    if not out.get("name") and not out.get("sku"):
        raise ValidationError({"mapping": ["Map at least the product name or SKU."]})
    return out


def mapped_columns(mapping: Mapping[str, Any]) -> list[str]:
    cols: list[str] = []
    for key in FIELDS:
        cols.extend(_columns(mapping.get(key)))
    return list(dict.fromkeys(cols))


def create_source(data: Mapping[str, Any], *, user_id, request=None) -> ProductSource:
    """POST /products/sources. The user must hold `query` on the dataset; masked/denied columns cannot be mapped."""
    from apps.datasets import services as datasets

    mapping = validate_mapping(data.get("mapping") or data.get("field_mapping"))
    try:
        dataset_id = UUID(str(data.get("dataset_id")))
    except ValueError as exc:
        raise ValidationError({"dataset_id": ["A dataset id is required."]}) from exc
    dataset, version, _policy, allowed, excluded = datasets.policy_columns(user_id, dataset_id, privilege="query")
    columns = mapped_columns(mapping)
    unknown = [c for c in columns if c not in set(allowed) | set(excluded)]
    if unknown:
        raise ValidationError({"mapping": [f"Unknown column(s): {', '.join(unknown)}."]})
    blocked = [c for c in columns if c in excluded]
    if blocked:
        raise ColumnNotAllowed(
            f"Column(s) {', '.join(blocked)} are masked or denied for you by the data policy and cannot be mapped.",
            extra={"excluded_by_policy": blocked})
    name = str(data.get("name") or dataset.name).strip()[:200]
    system = slugify(str(data.get("source_system") or name))[:64] or "source"
    if ProductSource.objects.filter(source_system=system).exists():
        raise Conflict(f"A product source named {system!r} already exists.", code="product_source_exists")
    try:
        priority = int(data.get("priority", 100))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"priority": ["An integer (lower = more trusted)."]}) from exc
    source = ProductSource.objects.create(name=name, source_system=system, dataset_id=dataset.id,
                                          field_mapping=mapping, priority=max(0, min(priority, 10_000)),
                                          created_by=user_id, last_version_id=version.id)
    audit.record("products.source.create", target=source, request=request,
                 metadata={"dataset_id": str(dataset.id), "mapping": mapping, "source_system": system})
    return source


def delete_source(source: ProductSource, *, request=None) -> None:
    audit.record("products.source.delete", target=source, request=request,
                 metadata={"source_system": source.source_system, "records": source.record_count})
    source.delete()


def source_payload(s: ProductSource) -> dict:
    return {"id": str(s.id), "name": s.name, "source_system": s.source_system, "dataset_id": str(s.dataset_id),
            "mapping": s.field_mapping, "priority": s.priority, "status": s.status, "last_error": s.last_error,
            "record_count": s.record_count,
            "last_synced_at": s.last_synced_at.isoformat() if s.last_synced_at else None,
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}


# ----------------------------------------------------------------------------------------------- runs
def start_run(kind: str, *, user_id, request=None, options: Mapping[str, Any] | None = None):
    """POST /products/normalize | /products/enrich -> 202. One run of a kind at a time per org."""
    from .tasks import enrich_products, normalize_products

    if kind == "normalize" and not ProductSource.objects.exists():
        raise NoProductSources()
    if kind == "enrich" and not ProductMaster.objects.exists():
        raise NoProductMasters()
    active = ProductRun.objects.filter(kind=kind, status__in=("queued", "running"),
                                       updated_at__gte=timezone.now() - RUN_STALE_AFTER).first()
    if active is not None:
        raise Conflict(f"Product {kind} is already running.", code=f"products_{kind}_running",
                       extra={"run_id": str(active.id), "job_id": str(active.job_id) if active.job_id else None})
    job = jobs.create_job(f"products.{kind}", user_id=user_id, message="Queued")
    run = ProductRun.objects.create(kind=kind, job_id=job.id, requested_by=user_id, options=dict(options or {}))
    audit.record(f"products.{kind}.start", target=run, request=request, metadata={"job_id": str(job.id)})
    kwargs = {"org_id": str(require_org_id()), "run_id": str(run.id), "actor_id": str(user_id) if user_id else None}
    task = normalize_products if kind == "normalize" else enrich_products
    transaction.on_commit(lambda: task.delay(**kwargs))
    return run, job


def run_payload(r: ProductRun) -> dict:
    return {"id": str(r.id), "kind": r.kind, "status": r.status, "job_id": str(r.job_id) if r.job_id else None,
            "requested_by": str(r.requested_by) if r.requested_by else None, "options": r.options, "stats": r.stats,
            "error": r.error, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat()}


def _progress(job_id, progress: int, message: str) -> None:
    jobs.update(job_id, status="running", progress=progress, message=message)
    jobs.publish(job_id, "log", {"message": message})


def _execute(org_id, run_id, body) -> dict | None:
    org = UUID(str(org_id))
    with tenant_step(org):
        run = ProductRun.objects.get(id=run_id)
        if run.status not in ("queued", "running"):
            return None
        run.status, run.started_at = "running", timezone.now()
        run.save(update_fields=["status", "started_at", "updated_at"])
        job_id, user_id, kind = run.job_id, run.requested_by, run.kind
    try:
        stats = body(org, run, job_id, user_id)
        with tenant_step(org):
            stats = _jsonable(stats)
            ProductRun.objects.filter(id=run_id).update(status="succeeded", finished_at=timezone.now(), stats=stats,
                                                        updated_at=timezone.now())
            result = {"run_id": str(run_id), **stats}
            jobs.finish(job_id, result=result)
            audit.record(f"products.{kind}.finish", target_type="products.productrun", target_id=str(run_id),
                         metadata={k: stats.get(k) for k in ("records", "masters", "pending_review", "proposed")})
            _notify(user_id, kind, ok=True, stats=stats)
        return result
    except Exception as exc:  # noqa: BLE001 - the run and job carry a stable code; details go to the log
        log.exception("product %s failed run=%s", kind, run_id)
        code = getattr(exc, "default_code", None) if isinstance(exc, NexoraError) else f"products_{kind}_failed"
        message = str(getattr(exc, "detail", "")) if isinstance(exc, NexoraError) else f"Product {kind} failed."
        with tenant_step(org):
            ProductRun.objects.filter(id=run_id).update(status="failed", finished_at=timezone.now(),
                                                        error={"code": code, "message": message})
            jobs.fail(job_id, code=code or f"products_{kind}_failed", message=message)
            audit.record(f"products.{kind}.failed", target_type="products.productrun", target_id=str(run_id),
                         metadata={"code": code})
            _notify(user_id, kind, ok=False, message=message)
        return None


def _notify(user_id, kind: str, *, ok: bool, stats: Mapping | None = None, message: str = "") -> None:
    from apps.datasets.services import notify_users

    if not user_id:
        return
    if not ok:
        notify_users([user_id], NOTIFY_FAILED, f"Product {kind} failed", message, "/products")
        return
    stats = stats or {}
    if kind == "normalize":
        body = (f"{stats.get('masters', 0):,} products from {stats.get('records', 0):,} records; "
                f"{stats.get('pending_review', 0):,} match(es) await review.")
        link = "/products/matches" if stats.get("pending_review") else "/products"
    else:
        body = f"{stats.get('proposed', 0):,} correction(s) proposed for review."
        link = "/products/corrections"
    notify_users([user_id], NOTIFY_FINISHED, f"Product {kind} finished", body, link)


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


# ----------------------------------------------------------------------------------------------- normalize
def run_normalize(*, org_id, run_id) -> dict | None:
    def body(org, run, job_id, user_id):
        stats: dict[str, Any] = {"sources": {}}
        with tenant_step(org):
            _progress(job_id, 5, "Reading product sources")
            cfg = get_settings()
            sources = list(ProductSource.objects.order_by("priority", "created_at"))
        frames = {}
        for source in sources:
            frames[source.id], stats["sources"][source.source_system] = _load_source(org, source, user_id)
        with tenant_step(org):
            _progress(job_id, 30, "Normalizing records")
            known = M.known_brands_of(raw for rows in frames.values() if rows for raw in rows.values())
            for source in sources:
                rows = frames.get(source.id)
                if rows is not None:
                    stats["sources"][source.source_system].update(
                        sync_source_records(source, rows, aliases=cfg.brand_aliases, known_brands=known))
            _progress(job_id, 55, "Matching duplicates and variants")
            stats.update(rebuild_masters(cfg))
            _progress(job_id, 85, "Indexing products for search")
            stats["embedded"], stats["search_mode"] = embed_masters()
        return stats

    return _execute(org_id, run_id, body)


def _load_source(org: UUID, source: ProductSource, user_id) -> tuple[dict | None, dict]:
    """{source_key: raw mapped values} under the requester's policy; None + error when no longer allowed."""
    from apps.datasets import services as datasets
    from apps.datasets.exceptions import DatasetNotReady

    mapping = source.field_mapping
    columns = mapped_columns(mapping)

    def fail(code, message, **extra):
        with tenant_step(org):
            ProductSource.objects.filter(id=source.id).update(
                status="error", last_error={"code": code, "message": message, **extra}, updated_at=timezone.now())
        return None, {"status": "error", "code": code, "message": message, **extra}

    try:
        with tenant_step(org):
            governed = datasets.governed_frame(user_id, source.dataset_id, columns)
    except NotFound:
        return fail("dataset_not_accessible", "The dataset no longer exists or you may not query it.")
    except DatasetNotReady:
        return fail("dataset_not_ready", "The dataset has no ready version.")
    except ValidationError:
        return fail("excluded_by_policy", "Every mapped column is masked or denied for you.",
                    excluded_by_policy=columns)
    if governed.excluded_by_policy:
        return fail("excluded_by_policy", "Mapped columns are now masked or denied for you; update the mapping.",
                    excluded_by_policy=list(governed.excluded_by_policy))
    rows: dict[str, dict] = {}
    duplicates = 0
    for row in governed.frame.iter_rows(named=True):
        raw = raw_values(mapping, row)
        key = source_key(raw)
        if key in rows:
            duplicates += 1
            continue
        rows[key] = raw
    with tenant_step(org):
        ProductSource.objects.filter(id=source.id).update(last_version_id=governed.version.id)
    return rows, {"status": "active", "records": len(rows), "duplicate_keys": duplicates,
                  "rows_filtered_by_policy": governed.rows_filtered}


def raw_values(mapping: Mapping[str, Any], row: Mapping[str, Any]) -> dict:
    raw: dict[str, Any] = {}
    for key in FIELDS:
        cols = _columns(mapping.get(key))
        if not cols:
            continue
        if key == "attributes":
            raw[key] = {c: row.get(c) for c in cols if row.get(c) not in (None, "")}
        else:
            raw[key] = row.get(cols[0])
    return _jsonable(raw)


def source_key(raw: Mapping[str, Any]) -> str:
    for key in ("external_id", "sku"):
        value = M._text(raw.get(key))
        if value:
            return f"{key}:{value}"[:255]
    digest = hashlib.sha1(repr(sorted((k, str(v)) for k, v in raw.items())).encode(), usedforsecurity=False)
    return f"row:{digest.hexdigest()[:32]}"


def sync_source_records(source: ProductSource, rows: Mapping[str, dict], *, aliases=None, known_brands=()) -> dict:
    existing = dict(ProductSourceRecord.objects.filter(source_id=source.id).values_list("source_key", "id"))
    stale = [rid for k, rid in existing.items() if k not in rows]
    for i in range(0, len(stale), BATCH):
        ProductSourceRecord.objects.filter(id__in=stale[i:i + BATCH]).delete()
    creates, updates = [], []
    for key, raw in rows.items():
        rec = ProductSourceRecord(org_id=require_org_id(), source_id=source.id, source_system=source.source_system,
                                  source_key=key, dataset_id=source.dataset_id, attributes=raw,
                                  normalized=_jsonable(M.normalize_record(raw, aliases=aliases,
                                                                          known_brands=known_brands)))
        if key in existing:
            rec.id = existing[key]
            updates.append(rec)
        else:
            creates.append(rec)
    ProductSourceRecord.objects.bulk_create(creates, batch_size=BATCH)
    ProductSourceRecord.objects.bulk_update(updates, ["attributes", "normalized", "source_system", "dataset_id"],
                                            batch_size=BATCH)
    ProductSource.objects.filter(id=source.id).update(status="active", last_error=None, record_count=len(rows),
                                                      last_synced_at=timezone.now(), updated_at=timezone.now())
    return {"created": len(creates), "updated": len(updates), "removed": len(stale)}


def _members(records) -> list[M.Member]:
    priorities = dict(ProductSource.objects.values_list("id", "priority"))
    return [M.Member(r.id, r.source_system, priorities.get(r.source_id, 100), r.normalized or {}) for r in records]


def _apply_master(master: ProductMaster, records: list[ProductSourceRecord]) -> list[M.Member]:
    members = _members(records)
    values = M.master_values(members) if members else {}
    for field, value in (master.overrides or {}).items():
        if field.startswith("attributes."):
            values.setdefault("attributes", {})[field.split(".", 1)[1]] = value
        else:
            values[field] = value
    values = _jsonable(values)
    master.values = values
    master.sku_normalized = str(values.get("sku") or "")[:64]
    master.name = str(values.get("name") or "")[:500]
    master.brand = str(values.get("brand") or "")[:200]
    master.category = str(values.get("category") or "")[:200]
    master.size = str(values.get("size") or "")[:40]
    price = values.get("price")
    try:
        master.price = Decimal(str(price)).quantize(Decimal("0.01")) if price not in (None, "") else None
    except ArithmeticError:
        master.price = None
    master.sources = sorted({r.source_system for r in records})
    master.record_count = len(records)
    attrs = " ".join(f"{k} {v}" for k, v in (values.get("attributes") or {}).items())
    master.search_text = " ".join(x for x in (master.name, master.brand, master.category, master.size,
                                              master.sku_normalized, attrs) if x)[:4000]
    return members


def _write_quality_and_attributes(plan: list[tuple[ProductMaster, list[M.Member]]]) -> None:
    ids = [m.id for m, _ in plan]
    for i in range(0, len(ids), BATCH):
        ProductAttribute.objects.filter(master_id__in=ids[i:i + BATCH]).delete()
        ProductQualityScore.objects.filter(master_id__in=ids[i:i + BATCH]).delete()
    attrs, scores, changed = [], [], []
    org = require_org_id()
    for master, members in plan:
        supplied = set()
        for m in members:
            supplied.update((m.normalized.get("attributes") or {}).keys())
        for key, value in (master.values.get("attributes") or {}).items():
            origin = ("correction" if f"attributes.{key}" in (master.overrides or {})
                      else "supplied" if key in supplied else "extracted")
            attrs.append(ProductAttribute(org_id=org, master_id=master.id, key=str(key)[:80],
                                          value=str(value)[:500], origin=origin))
        q = M.quality_score(master.values, members)
        scores.append(ProductQualityScore(org_id=org, master_id=master.id, completeness=Decimal(str(q["completeness"])),
                                          validity=Decimal(str(q["validity"])),
                                          consistency=Decimal(str(q["consistency"])),
                                          overall=Decimal(str(q["overall"])), issues=q["issues"]))
        master.quality = Decimal(str(q["overall"]))
        changed.append(master)
    ProductAttribute.objects.bulk_create(attrs, batch_size=BATCH)
    ProductQualityScore.objects.bulk_create(scores, batch_size=BATCH)
    ProductMaster.objects.bulk_update(changed, ["quality"], batch_size=BATCH)


MASTER_FIELDS = ["values", "sku_normalized", "name", "brand", "category", "size", "price", "sources", "record_count",
                 "search_text"]


def rebuild_masters(cfg: ProductSettings | None = None) -> dict:
    """Re-match every stored record (reviewer decisions honoured) and reconcile masters (ids stay stable)."""
    from apps.quality.services import pair_key, reconcile_clusters

    cfg = cfg or get_settings()
    records = {r.id: r for r in ProductSourceRecord.objects.all()}
    existing = {pair_key(a, b): (mid, st) for mid, a, b, st in
                ProductMatch.objects.values_list("id", "record_a_id", "record_b_id", "status")}
    result = M.resolve({rid: r.normalized for rid, r in records.items()}, weights=cfg.weights,
                       auto=float(cfg.auto_threshold), review=float(cfg.review_threshold),
                       confirmed=[p for p, (_m, s) in existing.items() if s == "confirmed"],
                       rejected=[p for p, (_m, s) in existing.items() if s == "rejected"])
    keep, creates, updates = set(), [], []
    for pair, (score, status) in result.matches.items():
        features = _jsonable({**score.features, "rules": list(score.rules)})
        current = existing.get(pair)
        if current is None:
            creates.append(ProductMatch(org_id=require_org_id(), record_a_id=pair[0], record_b_id=pair[1],
                                        score=Decimal(str(round(score.score, 4))), features=features, status=status))
        else:
            keep.add(current[0])
            if current[1] in ("auto_merged", "pending_review"):
                updates.append(ProductMatch(id=current[0], score=Decimal(str(round(score.score, 4))),
                                            features=features, status=status))
    stale = [mid for (mid, st) in existing.values() if st in ("auto_merged", "pending_review") and mid not in keep]
    for i in range(0, len(stale), BATCH):
        ProductMatch.objects.filter(id__in=stale[i:i + BATCH]).delete()
    ProductMatch.objects.bulk_create(creates, batch_size=BATCH)
    ProductMatch.objects.bulk_update(updates, ["score", "features", "status"], batch_size=BATCH)

    previous = {rid: r.master_id for rid, r in records.items() if r.master_id}
    assignments = reconcile_clusters(result.clusters, previous)
    masters = ProductMaster.objects.in_bulk([mid for mid, _c in assignments if mid])
    used, new, changed, plan = set(), [], [], []
    for mid, cluster in assignments:
        master = masters.get(mid) if mid else None
        if master is None:
            master = ProductMaster(org_id=require_org_id())
            new.append(master)
        else:
            used.add(master.id)
            changed.append(master)
        members = _apply_master(master, [records[r] for r in cluster])
        plan.append((master, members, cluster))
    gone = [mid for mid in ProductMaster.objects.values_list("id", flat=True) if mid not in used]
    for i in range(0, len(gone), BATCH):
        ProductMaster.objects.filter(id__in=gone[i:i + BATCH]).delete()
    ProductMaster.objects.bulk_create(new, batch_size=BATCH)
    ProductMaster.objects.bulk_update(changed, MASTER_FIELDS, batch_size=BATCH)
    for master, _members, cluster in plan:
        recs = [records[r] for r in cluster]
        for r in recs:
            r.master_id = master.id
    ProductSourceRecord.objects.bulk_update(list(records.values()), ["master"], batch_size=BATCH)
    _write_quality_and_attributes([(m, mem) for m, mem, _c in plan])
    return {**result.stats, "masters": len(plan), "masters_created": len(new), "masters_removed": len(gone)}


def _rebuild(master_ids) -> None:
    by_master = defaultdict(list)
    for r in ProductSourceRecord.objects.filter(master_id__in=list(master_ids)):
        by_master[r.master_id].append(r)
    plan = []
    for master in ProductMaster.objects.filter(id__in=list(master_ids)):
        plan.append((master, _apply_master(master, by_master.get(master.id, []))))
        master.embedding = None
        master.save()
    _write_quality_and_attributes(plan)


# ----------------------------------------------------------------------------------------------- search
def embed_masters(limit: int = 50_000) -> tuple[int, str]:
    """Embed masters without a vector (local model). Returns (count, search_mode)."""
    from apps.ai.embeddings import EmbeddingUnavailable, embed_passages, embedding_available

    if not embedding_available():
        return 0, "text"
    pending = list(ProductMaster.objects.filter(embedding__isnull=True).exclude(search_text="")
                   .only("id", "search_text")[:limit])
    done = 0
    try:
        for i in range(0, len(pending), 256):
            chunk = pending[i:i + 256]
            vectors = embed_passages([m.search_text for m in chunk])
            for m, vec in zip(chunk, vectors, strict=True):
                m.embedding = vec
            ProductMaster.objects.bulk_update(chunk, ["embedding"])
            done += len(chunk)
    except EmbeddingUnavailable:
        return done, "text"
    return done, "semantic"


def search(query: str, *, k: int = 20, filters: Mapping[str, Any] | None = None) -> dict:
    """POST /products/search: semantic (local embeddings) with a text fallback; `search_mode` says which ran."""
    from apps.ai.embeddings import EmbeddingUnavailable, embed_query, embedding_available
    from apps.ai.vectors import nearest

    query = " ".join(str(query or "").split())
    if not query:
        raise ValidationError({"query": ["A search query is required."]})
    if len(query) > 500:
        raise ValidationError({"query": ["At most 500 characters."]})
    try:
        k = max(1, min(int(k), SEARCH_MAX_K))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"k": [f"An integer 1-{SEARCH_MAX_K}."]}) from exc
    qs = ProductMaster.objects.all()
    for field in ("brand", "category"):
        value = (filters or {}).get(field)
        if value:
            qs = qs.filter(**{f"{field}__iexact": str(value)})
    reason = None
    if not embedding_available():
        reason = "model_unavailable"
    elif not qs.filter(embedding__isnull=False).exists():
        reason = "not_indexed"
    else:
        try:
            vec = embed_query(query)
            hits = nearest(qs, "embedding", vec, k)
            return {"query": query, "search_mode": "semantic", "reason": None,
                    "items": [{**master_payload(m), "score": round(1 - float(m.distance), 4)} for m in hits]}
        except EmbeddingUnavailable:
            reason = "model_unavailable"
    terms = [t for t in query.split() if len(t) > 1][:8] or [query]
    cond = Q()
    for t in terms:
        cond |= Q(search_text__icontains=t)
    candidates = list(qs.filter(cond)[:500])
    folded = [t.lower() for t in terms]

    def rank(m):
        text = m.search_text.lower()
        return sum(t in text for t in folded) / len(folded)

    ranked = sorted(candidates, key=lambda m: (-rank(m), m.name))[:k]
    return {"query": query, "search_mode": "text", "reason": reason,
            "items": [{**master_payload(m), "score": round(rank(m), 4)} for m in ranked]}


# ----------------------------------------------------------------------------------------------- payloads
def master_payload(m: ProductMaster) -> dict:
    return {"id": str(m.id), "sku": m.sku_normalized or None, "name": m.name, "brand": m.brand or None,
            "category": m.category or None, "size": m.size or None,
            "price": float(m.price) if m.price is not None else None, "record_count": m.record_count,
            "sources": m.sources, "quality": float(m.quality) if m.quality is not None else None,
            "updated_at": m.updated_at.isoformat()}


def record_payload(r: ProductSourceRecord) -> dict:
    return {"id": str(r.id), "source_system": r.source_system, "source_key": r.source_key,
            "attributes": r.attributes, "normalized": r.normalized,
            "master_id": str(r.master_id) if r.master_id else None}


def master_detail(m: ProductMaster) -> dict:
    q = ProductQualityScore.objects.filter(master_id=m.id).first()
    records = list(ProductSourceRecord.objects.filter(master_id=m.id).order_by("source_system", "source_key"))
    ids = [r.id for r in records]
    matches = ProductMatch.objects.filter(Q(record_a_id__in=ids) | Q(record_b_id__in=ids)).order_by("-score")[:50]
    return {**master_payload(m), "values": m.values, "overrides": m.overrides,
            "quality_detail": quality_payload(q) if q else None,
            "attributes": [attribute_payload(a) for a in m.attribute_rows.order_by("key")],
            "records": [record_payload(r) for r in records],
            "matches": matches_payload(list(matches)),
            "corrections": [correction_payload(c) for c in m.corrections.order_by("-created_at")[:50]]}


def quality_payload(q: ProductQualityScore) -> dict:
    return {"completeness": float(q.completeness), "validity": float(q.validity),
            "consistency": float(q.consistency), "overall": float(q.overall), "issues": q.issues}


def attribute_payload(a: ProductAttribute) -> dict:
    return {"key": a.key, "value": a.value, "origin": a.origin}


def matches_payload(matches: list[ProductMatch]) -> list[dict]:
    ids = {m.record_a_id for m in matches} | {m.record_b_id for m in matches}
    records = ProductSourceRecord.objects.in_bulk(list(ids))

    def rec(rid):
        r = records.get(rid)
        return record_payload(r) if r else {"id": str(rid)}

    return [{"id": str(m.id), "score": float(m.score), "status": m.status, "features": m.features,
             "record_a": rec(m.record_a_id), "record_b": rec(m.record_b_id),
             "reviewed_by": str(m.reviewed_by) if m.reviewed_by else None,
             "decided_at": m.decided_at.isoformat() if m.decided_at else None, "note": m.note,
             "created_at": m.created_at.isoformat()} for m in matches]


def correction_payload(c: ProductCorrection) -> dict:
    return {"id": str(c.id), "master_id": str(c.master_id), "field": c.field, "old_value": c.old_value,
            "new_value": c.new_value, "reason": c.reason,
            "confidence": float(c.confidence) if c.confidence is not None else None, "origin": c.origin,
            "status": c.status, "proposed_by": str(c.proposed_by) if c.proposed_by else None,
            "decided_by": str(c.decided_by) if c.decided_by else None,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None,
            "applied_at": c.applied_at.isoformat() if c.applied_at else None, "note": c.note,
            "created_at": c.created_at.isoformat()}


# ----------------------------------------------------------------------------------------------- review
def decide_match(match: ProductMatch, decision: str, *, user_id, request=None, note: str = "") -> ProductMatch:
    """confirm: the two records are the same product (masters merged); reject: never (master split)."""
    from apps.quality.services import UnionFind, pair_key

    if decision not in ("confirm", "reject"):
        raise ValidationError({"decision": ["Must be confirm or reject."]})
    with transaction.atomic():
        match = ProductMatch.objects.select_for_update().get(id=match.id)
        before = match.status
        match.status = "confirmed" if decision == "confirm" else "rejected"
        match.reviewed_by, match.decided_at, match.note = user_id, timezone.now(), str(note or "")[:500]
        match.save(update_fields=["status", "reviewed_by", "decided_at", "note", "updated_at"])
        ra, rb = ProductSourceRecord.objects.get(id=match.record_a_id), ProductSourceRecord.objects.get(
            id=match.record_b_id)
        effect: dict[str, Any] = {}
        if decision == "confirm" and ra.master_id and rb.master_id and ra.master_id != rb.master_id:
            keep, other = sorted([ra.master_id, rb.master_id], key=str)
            kept, gone = ProductMaster.objects.get(id=keep), ProductMaster.objects.get(id=other)
            kept.overrides = {**(gone.overrides or {}), **(kept.overrides or {})}  # applied corrections survive
            kept.save(update_fields=["overrides", "updated_at"])
            ProductSourceRecord.objects.filter(master_id=other).update(master_id=keep)
            ProductCorrection.objects.filter(master_id=other).update(master_id=keep)
            ProductMaster.objects.filter(id=other).delete()
            _rebuild([keep])
            effect = {"merged_into": keep, "removed_master": other}
        elif decision == "reject" and ra.master_id and ra.master_id == rb.master_id:
            master_id = ra.master_id
            rec_ids = list(ProductSourceRecord.objects.filter(master_id=master_id).values_list("id", flat=True))
            uf = UnionFind(rec_ids)
            pairs = ProductMatch.objects.filter(record_a_id__in=rec_ids, record_b_id__in=rec_ids)
            for a, b in pairs.filter(status="rejected").values_list("record_a_id", "record_b_id"):
                uf.forbid(a, b)
            for a, b in pairs.filter(status__in=("confirmed", "auto_merged")).order_by("-status", "-score") \
                    .values_list("record_a_id", "record_b_id"):
                uf.union(*pair_key(a, b))
            created = []
            for group in uf.groups()[1:]:
                new = ProductMaster.objects.create(org_id=require_org_id())
                ProductSourceRecord.objects.filter(id__in=group).update(master_id=new.id)
                created.append(new.id)
            _rebuild([master_id, *created])
            effect = {"split_master": master_id, "new_masters": created}
        audit.record(f"products.match.{match.status}", target=match, request=request,
                     changes={"before": {"status": before}, "after": {"status": match.status}},
                     metadata={"score": float(match.score), **_jsonable(effect)})
    return match


def _coerce(field: str, value: Any) -> Any:
    if field == "price":
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"new_value": ["The price must be a number."]}) from exc
        if not 0 < price < 1e9:
            raise ValidationError({"new_value": ["The price must be positive."]})
        return round(price, 2)
    if field == "sku":
        sku = M.normalize_sku(value)
        if not sku:
            raise ValidationError({"new_value": ["A SKU is required."]})
        return sku
    text = " ".join(str(value or "").split())[:500]
    if not text:
        raise ValidationError({"new_value": ["A value is required."]})
    return text


def propose_correction(master: ProductMaster, field: str, value: Any, *, user_id=None, reason: str = "",
                       confidence: float | None = None, origin: str = "user", request=None) -> ProductCorrection:
    if field not in CORRECTABLE and not (field.startswith("attributes.") and 1 < len(field) <= 80):
        raise ValidationError({"field": [f"One of {', '.join(CORRECTABLE)} or attributes.<key>."]})
    value = _coerce(field, value)
    old = (master.values.get("attributes") or {}).get(field.split(".", 1)[1]) if field.startswith("attributes.") \
        else master.values.get(field)
    if ProductCorrection.objects.filter(master_id=master.id, field=field, status="proposed",
                                        new_value=value).exists():
        raise Conflict("The same correction is already proposed.", code="correction_exists")
    c = ProductCorrection.objects.create(master=master, field=field, old_value=_jsonable(old), new_value=value,
                                         reason=str(reason)[:500], origin=origin, proposed_by=user_id,
                                         confidence=Decimal(str(round(confidence, 4))) if confidence else None)
    if origin == "user":
        audit.record("products.correction.propose", target=c, request=request,
                     metadata={"master_id": str(master.id), "field": field})
    return c


def decide_correction(c: ProductCorrection, decision: str, *, user_id, request=None,
                      note: str = "") -> ProductCorrection:
    """approve: the value is applied to the master (and kept across re-runs); reject: nothing changes."""
    if decision not in ("approve", "reject"):
        raise ValidationError({"decision": ["Must be approve or reject."]})
    with transaction.atomic():
        c = ProductCorrection.objects.select_for_update().get(id=c.id)
        if c.status != "proposed":
            raise Conflict(f"The correction is already {c.status}.", code="correction_decided")
        c.decided_by, c.decided_at, c.note = user_id, timezone.now(), str(note or "")[:500]
        before = None
        if decision == "approve":
            master = ProductMaster.objects.select_for_update().get(id=c.master_id)
            before = master.values.get(c.field)
            master.overrides = {**(master.overrides or {}), c.field: c.new_value}
            master.save(update_fields=["overrides", "updated_at"])
            _rebuild([master.id])
            c.status, c.applied_at = "applied", timezone.now()
        else:
            c.status = "rejected"
        c.save()
        audit.record(f"products.correction.{decision}", target=c, request=request,
                     changes={"before": {"status": "proposed", "value": _jsonable(before)},
                              "after": {"status": c.status, "value": c.new_value if decision == "approve" else None}},
                     metadata={"master_id": str(c.master_id), "field": c.field, "origin": c.origin})
    return c


# ----------------------------------------------------------------------------------------------- enrich
def run_enrich(*, org_id, run_id) -> dict | None:
    def body(org, run, job_id, user_id):
        with tenant_step(org):
            _progress(job_id, 10, "Analysing the catalog")
            masters = list(ProductMaster.objects.all())
            category_by_core, spelling = _catalog_hints(masters)
            open_keys = set(ProductCorrection.objects.filter(status__in=("proposed", "rejected"))
                            .values_list("master_id", "field"))
            proposed = 0
            org_id_ = require_org_id()
            batch = []
            for m in masters:
                for s in M.rule_suggestions(m.values, category_by_core=category_by_core, category_spelling=spelling):
                    if (m.id, s.field) in open_keys:
                        continue
                    open_keys.add((m.id, s.field))
                    current = m.values.get(s.field) if "." not in s.field else None
                    batch.append(ProductCorrection(org_id=org_id_, master_id=m.id, field=s.field,
                                                   old_value=_jsonable(current), new_value=_jsonable(s.value),
                                                   reason=s.reason[:500], confidence=Decimal(str(s.confidence)),
                                                   origin="rule", proposed_by=user_id))
            ProductCorrection.objects.bulk_create(batch, batch_size=BATCH)
            proposed = len(batch)
            return {"masters": len(masters), "proposed": proposed, "mode": "local"}

    return _execute(org_id, run_id, body)


def _catalog_hints(masters) -> tuple[dict, dict]:
    by_core: dict[str, Counter] = defaultdict(Counter)
    spellings: dict[str, Counter] = defaultdict(Counter)
    for m in masters:
        core, cat = m.values.get("core_name"), m.values.get("category")
        if core and cat:
            by_core[core][cat] += 1
        if cat:
            spellings[M.brand_key(cat)][cat] += 1
    category_by_core = {core: c.most_common(1)[0][0] for core, c in by_core.items()}
    spelling = {key: c.most_common(1)[0][0] for key, c in spellings.items() if len(c) > 1}
    return category_by_core, spelling
