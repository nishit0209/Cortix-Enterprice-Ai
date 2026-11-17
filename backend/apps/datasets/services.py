"""Public service API of apps.datasets. Other apps import only from here.

Uploads:   create_upload(file, user_id=...)                       -> Upload
Datasets:  create_dataset(name=..., upload_id=..., ...)           -> (Dataset, Job)   [202 + ingest task]
           add_version(dataset, upload_id=..., ...)               -> Job
           get_dataset(id) / set_classification / update_dataset / soft_delete
Ingest:    run_ingest(...)  (body of the `ingest_dataset` Celery task)
Data:      version_columns(version), load_version_frame(version), load_current_frame(dataset_id), preview(...)
Other:     dataset_classification(id) (registered asset classifier), data_stats(), parse_as_type(series, pg_type)
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import polars as pl
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Max, Sum
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs, storage
from apps.core.tenancy import require_org_id
from apps.core.tenancy.tasks import tenant_step
from apps.warehouse import naming as wh_naming
from apps.warehouse import services as warehouse
from apps.warehouse.exceptions import WarehouseError
from apps.warehouse.serialization import rows_to_jsonable

from .exceptions import (
    AttestationRequired,
    DatasetNotReady,
    StorageUnavailable,
    UploadRejected,
    UploadTooLarge,
)
from .ingest import filetypes, inference, parsing
from .ingest.analyze import AnalyzedTable, analyze
from .ingest.errors import IngestError
from .ingest.profiling import schema_drift
from .models import ATTESTED_CLASSIFICATIONS, CLASSIFICATIONS, Dataset, DatasetColumn, DatasetVersion, Upload

log = logging.getLogger(__name__)

PREVIEW_MAX = 1000
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------------------------------
def max_upload_bytes() -> int:
    return int(settings.MAX_UPLOAD_BYTES)


def safe_filename(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable())[:255]
    return base or "upload"


def create_upload(fileobj, *, user_id=None, request=None) -> Upload:
    """Validate (size, extension, magic bytes, zip-bomb), hash and stream a file to object storage."""
    size = int(getattr(fileobj, "size", 0) or 0)
    if size > max_upload_bytes():
        raise UploadTooLarge(f"The file is {size} bytes; the limit is {max_upload_bytes()} bytes.")
    if size == 0:
        raise UploadRejected("The file is empty.", code="upload_empty")
    filename = safe_filename(getattr(fileobj, "name", ""))
    raw = getattr(fileobj, "file", fileobj)
    try:
        kind = filetypes.detect_kind(filename, raw)
    except IngestError as exc:
        raise UploadRejected(exc.message, code=exc.code) from exc
    digest = hashlib.sha256()
    raw.seek(0)
    while True:
        block = raw.read(1024 * 1024)
        if not block:
            break
        digest.update(block)
    raw.seek(0)
    upload = Upload(
        filename=filename, content_type=filetypes.CONTENT_TYPES[kind], file_kind=kind, size_bytes=size,
        sha256=digest.hexdigest(), status="scanning", created_by=user_id,
    )
    key_name = _SAFE_NAME_RE.sub("_", filename)[:120] or "upload"
    upload.object_key = storage.org_key("uploads", str(upload.id), key_name)
    try:
        storage.put_object(upload.object_key, raw, upload.content_type)
    except (BotoCoreError, ClientError) as exc:
        log.warning("upload to object storage failed: %s", type(exc).__name__)
        raise StorageUnavailable() from exc
    upload.status = "ready"
    upload.save()
    audit.record("upload.create", target=upload, request=request,
                 metadata={"filename": filename, "size_bytes": size, "kind": kind, "sha256": upload.sha256})
    return upload


# ---------------------------------------------------------------------------------------------------
# datasets
# ---------------------------------------------------------------------------------------------------
def unique_slug(name: str) -> str:
    base = (slugify(name) or "dataset")[:200]
    slug, n = base, 2
    while Dataset.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def get_dataset(dataset_id, *, include_deleted: bool = False) -> Dataset:
    try:
        dataset_uuid = dataset_id if isinstance(dataset_id, UUID) else UUID(str(dataset_id))
    except ValueError as exc:
        raise NotFound() from exc
    qs = Dataset.objects.select_related("current_version")
    if not include_deleted:
        qs = qs.exclude(status="deleted")
    try:
        return qs.get(id=dataset_uuid)
    except Dataset.DoesNotExist as exc:
        raise NotFound() from exc


def _get_ready_upload(upload_id) -> Upload:
    try:
        upload = Upload.objects.get(id=upload_id)
    except (Upload.DoesNotExist, ValueError) as exc:
        raise ValidationError({"upload_id": ["Upload not found."]}) from exc
    if upload.status != "ready":
        raise UploadRejected(f"The upload is {upload.status} and cannot be ingested.", code="upload_not_ready")
    return upload


def _apply_classification(dataset: Dataset, classification: str, *, attest: bool, user_id) -> None:
    if classification not in dict(CLASSIFICATIONS):
        raise ValidationError({"classification": [f"Must be one of {', '.join(dict(CLASSIFICATIONS))}."]})
    if classification in ATTESTED_CLASSIFICATIONS:
        if attest is not True:
            raise AttestationRequired()
        if user_id is None:
            raise AttestationRequired("An attestation must be made by a user.")
        dataset.classification_attested_by = user_id
        dataset.classification_attested_at = timezone.now()
    else:
        dataset.classification_attested_by = None
        dataset.classification_attested_at = None
    dataset.classification = classification


def _enqueue_ingest(dataset: Dataset, upload: Upload, job, *, options: dict | None, user_id) -> None:
    from .tasks import ingest_dataset

    org_id = str(require_org_id())
    kwargs = {
        "org_id": org_id, "dataset_id": str(dataset.id), "upload_id": str(upload.id), "job_id": str(job.id),
        "options": options or {}, "actor_id": str(user_id) if user_id else None,
    }
    transaction.on_commit(lambda: ingest_dataset.delay(**kwargs))


def create_dataset(
    *,
    name: str,
    upload_id,
    classification: str | None = None,
    attest: bool = False,
    description: str = "",
    tags: Sequence[str] | None = None,
    options: dict | None = None,
    user_id=None,
    request=None,
):
    upload = _get_ready_upload(upload_id)
    dataset = Dataset(
        name=name.strip()[:200], slug=unique_slug(name), description=description or "", source_kind="upload",
        owner_id=user_id, status="processing", tags=list(tags or []),
    )
    _apply_classification(dataset, classification or "internal", attest=attest, user_id=user_id)
    job = jobs.create_job("dataset_ingest", user_id=user_id, message="Queued")
    dataset.last_job_id = job.id
    dataset.save()
    audit.record("dataset.create", target=dataset, request=request, metadata={
        "name": dataset.name, "upload_id": str(upload.id), "classification": dataset.classification,
        "attested": dataset.classification in ATTESTED_CLASSIFICATIONS, "job_id": str(job.id),
    })
    _enqueue_ingest(dataset, upload, job, options=options, user_id=user_id)
    return dataset, job


def add_version(dataset: Dataset, *, upload_id, options: dict | None = None, user_id=None, request=None):
    upload = _get_ready_upload(upload_id)
    job = jobs.create_job("dataset_ingest", user_id=user_id, message="Queued")
    dataset.last_job_id = job.id
    if dataset.current_version_id is None:
        dataset.status = "processing"
    dataset.save(update_fields=["last_job_id", "status", "updated_at"])
    audit.record("dataset.version.create", target=dataset, request=request,
                 metadata={"upload_id": str(upload.id), "job_id": str(job.id)})
    _enqueue_ingest(dataset, upload, job, options=options, user_id=user_id)
    return job


def update_dataset(dataset: Dataset, data: dict, *, request=None) -> Dataset:
    changes_before, changes_after = {}, {}
    for field in ("name", "description", "tags"):
        if field in data and getattr(dataset, field) != data[field]:
            changes_before[field] = getattr(dataset, field)
            setattr(dataset, field, data[field])
            changes_after[field] = data[field]
    if changes_after:
        dataset.save()
        audit.record("dataset.update", target=dataset, request=request,
                     changes={"before": changes_before, "after": changes_after})
    return dataset


def set_classification(dataset: Dataset, classification: str, *, attest: bool, user_id, request=None) -> Dataset:
    before = dataset.classification
    _apply_classification(dataset, classification, attest=attest, user_id=user_id)
    dataset.save()
    audit.record("dataset.classification.change", target=dataset, request=request,
                 changes={"before": {"classification": before}, "after": {"classification": dataset.classification}},
                 metadata={"attested": dataset.classification in ATTESTED_CLASSIFICATIONS})
    return dataset


def soft_delete(dataset: Dataset, *, request=None) -> None:
    dataset.status = "deleted"
    dataset.deleted_at = timezone.now()
    dataset.save(update_fields=["status", "deleted_at", "updated_at"])
    audit.record("dataset.delete", target=dataset, request=request, metadata={"name": dataset.name, "soft": True})


def dataset_classification(asset_id: UUID) -> str:
    """Registered with apps.core.registry: 'dataset:<uuid>' -> classification (needs a tenant context)."""
    value = Dataset.objects.filter(id=asset_id).values_list("classification", flat=True).first()
    return value or "confidential"


# ---------------------------------------------------------------------------------------------------
# versions & data access
# ---------------------------------------------------------------------------------------------------
def current_version(dataset: Dataset) -> DatasetVersion | None:
    return dataset.current_version if dataset.current_version_id else None


def require_ready_version(dataset: Dataset, version_no: int | None = None) -> DatasetVersion:
    if version_no is not None:
        version = DatasetVersion.objects.filter(dataset_id=dataset.id, version=version_no).first()
        if version is None:
            raise NotFound()
    else:
        version = current_version(dataset)
    if version is None or version.status != "ready":
        raise DatasetNotReady()
    return version


def version_columns(version: DatasetVersion) -> list[dict[str, Any]]:
    return [
        {
            "name": c.name, "source_name": c.source_name, "data_type": c.data_type, "semantic_type": c.semantic_type,
            "nullable": c.nullable, "is_pii": c.is_pii, "pii_type": c.pii_type, "ordinal": c.ordinal, "stats": c.stats,
        }
        for c in DatasetColumn.objects.filter(version_id=version.id).order_by("ordinal")
    ]


def _specs(columns: Sequence[dict]) -> list[warehouse.ColumnSpec]:
    return [warehouse.ColumnSpec(c["name"], c["data_type"]) for c in columns]


def load_version_frame(version: DatasetVersion, columns: Sequence[str] | None = None,
                       limit: int | None = None) -> pl.DataFrame:
    cols = version_columns(version)
    if columns is not None:
        wanted = set(columns)
        unknown = wanted - {c["name"] for c in cols}
        if unknown:
            raise ValidationError({"columns": [f"Unknown columns: {', '.join(sorted(unknown))}."]})
        cols = [c for c in cols if c["name"] in wanted]
    return warehouse.read_table(version.org_id, version.wh_table, _specs(cols), limit=limit)


def load_current_frame(dataset_id, columns: Sequence[str] | None = None) -> pl.DataFrame:
    dataset = get_dataset(dataset_id)
    return load_version_frame(require_ready_version(dataset), columns)


def preview(dataset: Dataset, *, limit: int = 100, version_no: int | None = None) -> dict:
    version = require_ready_version(dataset, version_no)
    limit = max(1, min(int(limit), PREVIEW_MAX))
    cols = version_columns(version)
    result = warehouse.preview_rows(version.org_id, version.wh_table, [c["name"] for c in cols], limit=limit)
    types = {c["name"]: c["data_type"] for c in cols}
    return {
        "version": version.version,
        "columns": [{"name": m.name, "type": types.get(m.name, m.type), "masked": m.masked} for m in result.columns],
        "rows": rows_to_jsonable(result.rows),
        "row_count": result.row_count,
        "truncated": result.truncated,
    }


def parse_as_type(series: pl.Series, pg_type: str) -> pl.Series:
    return inference.parse_as_type(series, pg_type)


# ---------------------------------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------------------------------
_stats_providers: list[Callable[[], dict]] = []


def register_stats_provider(fn: Callable[[], dict]) -> None:
    """Pipelines (wave 2) contribute {pipelines_active, runs_7d} to /data-stats through this hook."""
    _stats_providers.append(fn)


def data_stats() -> dict:
    live = Dataset.objects.exclude(status="deleted")
    agg = live.aggregate(rows=Sum("row_count"), quality=Avg("quality_score"))
    wh_bytes = DatasetVersion.objects.filter(status__in=("ready", "loading"), dataset__in=live).aggregate(
        b=Sum("byte_size"))["b"] or 0
    upload_bytes = Upload.objects.filter(status="ready").aggregate(b=Sum("size_bytes"))["b"] or 0
    out = {
        "datasets": live.count(),
        "rows_total": int(agg["rows"] or 0),
        "avg_quality": round(float(agg["quality"]), 2) if agg["quality"] is not None else None,
        "pipelines_active": 0,
        "runs_7d": {"succeeded": 0, "failed": 0},
        "storage_bytes": int(wh_bytes) + int(upload_bytes),
    }
    for provider in _stats_providers:
        try:
            out.update(provider())
        except Exception:  # noqa: BLE001 - a broken provider must not break the dashboard
            log.exception("data-stats provider failed")
    return out


# ---------------------------------------------------------------------------------------------------
# ingestion (task body)
# ---------------------------------------------------------------------------------------------------
STAGES = {
    "download": (5, "Downloading the file"),
    "scan": (10, "Scanning the file"),
    "parse": (20, "Parsing"),
    "profile": (40, "Detecting types and profiling"),
    "load": (60, "Loading into the warehouse"),
    "quality": (80, "Checking data quality"),
    "finalize": (95, "Publishing the dataset version"),
}


def _stage(job_id, stage: str, **extra) -> None:
    progress, message = STAGES[stage]
    jobs.update(job_id, status="running", progress=progress, message=message)
    jobs.publish(job_id, "log", {"stage": stage, "message": message, **extra})


def _column_rows(analyzed: AnalyzedTable) -> list[dict]:
    return [
        {"name": c.name, "source_name": c.source_name, "data_type": c.data_type, "semantic_type": c.semantic_type,
         "nullable": c.nullable, "is_pii": c.is_pii, "pii_type": c.pii_type, "pii_source": c.pii_source,
         "ordinal": c.ordinal, "stats": c.stats}
        for c in analyzed.columns
    ]


def run_ingest(*, org_id, dataset_id, upload_id, job_id, options: dict | None = None) -> dict | None:
    """Download -> scan -> parse -> infer/profile/PII -> warehouse load -> quality -> publish version.

    Runs with short tenant transactions (tenant_step) so no app-DB transaction spans the heavy work.
    Never raises for data problems: the job, dataset and version record the failure with a stable code.
    """
    from apps.lineage import services as lineage
    from apps.quality import services as quality

    org = UUID(str(org_id))
    workdir = tempfile.mkdtemp(prefix="nexora_ingest_")
    version_id = None
    try:
        with tenant_step(org):
            _stage(job_id, "download")
            upload = Upload.objects.get(id=upload_id)
            dataset = Dataset.objects.get(id=dataset_id)
            local = os.path.join(workdir, "source" + os.path.splitext(upload.filename)[1].lower())
            try:
                storage.download_to(upload.object_key, local)
            except (BotoCoreError, ClientError) as exc:
                raise IngestError("storage_unavailable", "The uploaded file could not be read from storage.") from exc
            _stage(job_id, "scan")
        with open(local, "rb") as fh:
            kind = filetypes.detect_kind(upload.filename, fh)
        if os.path.getsize(local) > max_upload_bytes():
            raise IngestError("upload_too_large", "The file is larger than the upload limit.")

        with tenant_step(org):
            _stage(job_id, "parse", format=kind)
        opts = options or {}
        parsed = parsing.parse_file(local, kind, parsing.ParseOptions(
            sheet=opts.get("sheet"), delimiter=opts.get("delimiter"), has_header=opts.get("has_header"),
            encoding=opts.get("encoding"),
        ))
        with tenant_step(org):
            _stage(job_id, "profile", rows=parsed.row_count, columns=parsed.frame.width)
        analyzed = analyze(parsed)
        columns = _column_rows(analyzed)

        with tenant_step(org):
            previous = (
                DatasetVersion.objects.filter(dataset_id=dataset.id, status="ready").order_by("-version").first()
            )
            number = (DatasetVersion.objects.filter(dataset_id=dataset.id).aggregate(m=Max("version"))["m"] or 0) + 1
            profile = dict(analyzed.profile)
            profile["version"] = number
            if previous is not None:
                prev_cols = [(c["name"], c["data_type"]) for c in version_columns(previous)]
                profile["drift"] = schema_drift(prev_cols, analyzed.schema)
            version = DatasetVersion.objects.create(
                dataset_id=dataset.id, version=number, upload_id=upload.id, job_id=job_id, status="loading",
                schema_hash=analyzed.schema_hash, profile=profile,
                wh_table=wh_naming.dataset_table_name(dataset.id, number),
            )
            version_id = version.id
            DatasetColumn.objects.bulk_create([
                DatasetColumn(
                    org_id=org, version_id=version.id, ordinal=c["ordinal"], name=c["name"],
                    source_name=c["source_name"][:500], data_type=c["data_type"], semantic_type=c["semantic_type"],
                    nullable=c["nullable"], is_pii=c["is_pii"], pii_type=c["pii_type"], pii_source=c["pii_source"],
                    stats=c["stats"],
                )
                for c in columns
            ])
            lineage.record("upload", upload.id, "dataset_version", version.id, label="ingested",
                           transform={"format": kind, "rows": analyzed.frame.height})
            _stage(job_id, "load", table=version.wh_table)

        with tenant_step(org):
            loaded = warehouse.load_table(
                org, version.wh_table, [warehouse.ColumnSpec(c["name"], c["data_type"]) for c in columns], analyzed.frame
            )

        with tenant_step(org):
            DatasetVersion.objects.filter(id=version.id).update(
                wh_schema=loaded.wh_schema, row_count=loaded.row_count, byte_size=loaded.byte_size
            )
            _stage(job_id, "quality")
            quality.ensure_suggested_rules(dataset, columns, profile)
            specs = quality.load_rule_specs(dataset.id)
        computation = quality.compute(analyzed.frame, columns, profile, specs, org_id=org)

        with tenant_step(org):
            _stage(job_id, "finalize")
            version.refresh_from_db()
            report = quality.persist(version, computation)
            version.status = "ready"
            version.ready_at = timezone.now()
            version.save(update_fields=["status", "ready_at", "updated_at"])
            dataset.refresh_from_db()
            blocking = computation.blocking_failures
            if blocking:
                dataset.last_error = {
                    "code": "quality_blocking_failure",
                    "message": "Blocking quality rules failed; the new version was not made current.",
                    "version": version.version, "rules": blocking,
                }
                if dataset.current_version_id is None:
                    dataset.status = "failed"
            else:
                dataset.current_version_id = version.id
                dataset.status = "ready"
                dataset.row_count = loaded.row_count
                dataset.column_count = len(columns)
                dataset.quality_score = report.score
                dataset.last_error = None
            dataset.save()
            result = {
                "dataset_id": str(dataset.id), "version_id": str(version.id), "version": version.version,
                "row_count": loaded.row_count, "column_count": len(columns), "quality_score": float(report.score),
                "wh_table": version.wh_table, "current": not blocking, "blocking_failures": blocking,
                "pii_columns": analyzed.profile.get("pii_columns", []),
            }
            audit.record("dataset.version.ready", target=version, metadata=result)
            jobs.finish(job_id, result=result)
        return result
    except IngestError as exc:
        _fail(org, dataset_id, upload_id, version_id, job_id, exc.code, exc.message, exc.detail)
    except WarehouseError as exc:
        code = exc.get_codes() if isinstance(exc.get_codes(), str) else "warehouse_error"
        _fail(org, dataset_id, upload_id, version_id, job_id, code, str(exc.detail))
    except Exception:  # noqa: BLE001 - never leak internals; the job carries a stable code
        log.exception("ingest failed dataset=%s", dataset_id)
        _fail(org, dataset_id, upload_id, version_id, job_id, "ingest_failed", "Unexpected error while ingesting the file.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return None


_UPLOAD_REJECT_CODES = {"upload_empty", "upload_type_not_allowed", "upload_content_mismatch", "upload_zip_bomb",
                        "upload_too_large", "malformed_file", "too_many_columns", "too_many_rows", "no_columns"}


def _fail(org: UUID, dataset_id, upload_id, version_id, job_id, code: str, message: str, detail: dict | None = None):
    with tenant_step(org):
        if version_id is not None:
            DatasetVersion.objects.filter(id=version_id).update(status="failed", error={"code": code, "message": message})
        dataset = Dataset.objects.filter(id=dataset_id).first()
        if dataset is not None:
            dataset.last_error = {"code": code, "message": message}
            if dataset.current_version_id is None:
                dataset.status = "failed"
            dataset.save(update_fields=["last_error", "status", "updated_at"])
        if code in _UPLOAD_REJECT_CODES:
            Upload.objects.filter(id=upload_id).update(status="rejected", reject_reason=message[:300])
        jobs.publish(job_id, "log", {"stage": "failed", "code": code, "message": message})
        jobs.fail(job_id, code=code, message=message, detail=detail or None)
        audit.record("dataset.ingest.failed", target_type="datasets.dataset", target_id=str(dataset_id),
                     metadata={"code": code, "message": message, "job_id": str(job_id)})
