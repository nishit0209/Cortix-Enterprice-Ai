"""Public service API of apps.connectors. Other apps import only from here.

Data sources:  create_source / update_source / delete_source / get_source / source_payload
Jobs (202):    start_test / start_introspect / start_import       -> Job (body runs in apps.connectors.tasks)
Task bodies:   run_test / run_introspect / run_import
Pipelines:     read_frame(source_id, table, ...) -> polars.DataFrame   (verified source, read-only re-check)
               write_frame_csv(frame, path)                           (canonical CSV for the upload path)
               register_usage_checker(fn)                             (fn(source_id) -> list of user-facing names)

Secrets: the password is envelope-encrypted (apps.core.crypto) into `Credential` and decrypted ONLY inside
`build_connector`, i.e. in the worker that connects. It is never returned, logged or written to audit/job rows.
Outbound connections (test, introspect, import, pipeline reads) run only in Celery workers (egress isolation).
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

import polars as pl
from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, transaction
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit, jobs
from apps.core.crypto import decrypt_secret, encrypt_secret
from apps.core.exceptions import Conflict
from apps.core.tenancy import require_org_id
from apps.core.tenancy.tasks import tenant_step

from . import (
    postgres,  # noqa: F401 - registers the postgresql connector
    ssrf,
)
from .base import BaseConnector, Batch, TableInfo, connector_class
from .exceptions import ConnectorError, ImportTooLarge, SourceInUse, SourceNotReady
from .models import Credential, DataSource, SourceImport
from .values import to_text

log = logging.getLogger(__name__)

CONNECTION_FIELDS = ("config", "username", "password")
MAX_PASSWORD_LEN = 1024
IMPORT_BATCH_ROWS = 10_000
ATTESTED = ("synthetic", "public")
CLASSIFICATIONS = ("synthetic", "public", "internal", "confidential")

_usage_checkers: list[Callable[[UUID], list[str]]] = []


def register_usage_checker(fn: Callable[[UUID], list[str]]) -> None:
    """Higher apps (pipelines) report which of their objects use a data source (blocks deletion)."""
    _usage_checkers.append(fn)


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------
def _credential_aad(org_id, credential_id) -> str:
    return f"org:{org_id}:credential:{credential_id}"


def _store_password(password: str, credential: Credential | None = None) -> Credential:
    org_id = require_org_id()
    cred = credential or Credential(kind="db_password")
    aad = _credential_aad(org_id, cred.id)
    blob = encrypt_secret(password, aad=aad)
    cred.blob, cred.kek_id, cred.aad = blob, blob["kek_id"], aad
    if credential is not None:
        cred.rotated_at = timezone.now()
    cred.save()
    return cred


def _reveal_password(source: DataSource) -> str:
    if source.credential_id is None:
        return ""
    cred = Credential.objects.get(id=source.credential_id)
    return decrypt_secret(cred.blob, aad=_credential_aad(source.org_id, cred.id))


def validate_config(type_: str, config: dict[str, Any]) -> dict[str, Any]:
    cls = connector_class(type_)
    try:
        model = cls.config_schema.model_validate(config or {})
    except PydanticValidationError as exc:
        errors: dict[str, list[str]] = {}
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__") or "config"
            msg = str(err.get("msg", "Invalid value.")).removeprefix("Value error, ")
            errors.setdefault(f"config.{loc}" if loc != "config" else "config", []).append(msg)
        raise ValidationError(errors) from None
    public = model.public() if hasattr(model, "public") else model.model_dump()
    ssrf.precheck_host(public["host"])  # network-free: IP literals and internal names fail fast
    return public


def get_source(source_id) -> DataSource:
    try:
        sid = source_id if isinstance(source_id, UUID) else UUID(str(source_id))
    except ValueError as exc:
        raise NotFound() from exc
    try:
        return DataSource.objects.get(id=sid)
    except DataSource.DoesNotExist as exc:
        raise NotFound() from exc


def _stats_cutoff():
    from apps.datasets.services import stats_cutoff

    return stats_cutoff()


def stats() -> dict[str, Any]:
    """data-stats contribution (registered with datasets.register_stats_provider): data sources from the DB."""
    return {
        "sources": DataSource.objects.count(),
        "sources_active": DataSource.objects.filter(status="active").count(),
        # sources that existed at the previous-period cutoff and still exist (deleted sources leave no trace)
        "sources_prev": DataSource.objects.filter(created_at__lte=_stats_cutoff()).count(),
    }


def source_exists(source_id) -> bool:
    try:
        return DataSource.objects.filter(id=UUID(str(source_id))).exists()
    except ValueError:
        return False


def _job_kwargs(source: DataSource, job, user_id) -> dict[str, Any]:
    return {"org_id": str(require_org_id()), "source_id": str(source.id), "job_id": str(job.id),
            "actor_id": str(user_id) if user_id else None}


# ---------------------------------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------------------------------
def create_source(*, name: str, type: str, config: dict, username: str, password: str, user_id=None,
                  request=None):
    """Save a data source (status `pending`) and queue the connection + read-only test. -> (source, job)."""
    if not password or len(password) > MAX_PASSWORD_LEN:
        raise ValidationError({"password": ["A password of at most 1024 characters is required."]})
    public = validate_config(type, config)
    if DataSource.objects.filter(name=name).exists():
        raise Conflict("A data source with this name already exists.", code="data_source_name_taken")
    cred = _store_password(password)
    source = DataSource(name=name, type=type, config=public, username=username, credential=cred,
                        status="pending", created_by=user_id)
    try:
        with transaction.atomic():
            source.save()
    except IntegrityError as exc:
        raise Conflict("A data source with this name already exists.", code="data_source_name_taken") from exc
    audit.record("data_source.create", target=source, request=request,
                 metadata={"name": name, "type": type, "config": public, "username": username})
    job = start_test(source, user_id=user_id, request=None, audit_event=False)
    return source, job


def update_source(source: DataSource, data: dict[str, Any], *, user_id=None, request=None):
    """PATCH. Changing connection fields resets verification and queues a new test. -> (source, job|None)."""
    before, after = {}, {}
    connection_changed = False
    if "name" in data and data["name"] != source.name:
        if DataSource.objects.filter(name=data["name"]).exclude(id=source.id).exists():
            raise Conflict("A data source with this name already exists.", code="data_source_name_taken")
        before["name"], after["name"] = source.name, data["name"]
        source.name = data["name"]
    if "config" in data:
        merged = {**(source.config or {}), **(data["config"] or {})}
        public = validate_config(source.type, merged)
        if public != source.config:
            before["config"], after["config"] = source.config, public
            source.config = public
            connection_changed = True
    if "username" in data and data["username"] != source.username:
        before["username"], after["username"] = source.username, data["username"]
        source.username = data["username"]
        connection_changed = True
    if data.get("password"):
        if len(data["password"]) > MAX_PASSWORD_LEN:
            raise ValidationError({"password": ["At most 1024 characters."]})
        cred = Credential.objects.filter(id=source.credential_id).first() if source.credential_id else None
        source.credential = _store_password(data["password"], cred)
        after["auth"] = "rotated"  # the secret itself never enters audit rows
        connection_changed = True
    if "status" in data and data["status"] in ("disabled", "pending") and data["status"] != source.status:
        before["status"], after["status"] = source.status, data["status"]
        source.status = data["status"]
        if data["status"] == "pending":
            connection_changed = True
    if connection_changed:
        source.status = "pending" if source.status != "disabled" else "disabled"
        source.verified_readonly = False
        source.resolved_ip = None
    if not after:
        return source, None
    try:
        with transaction.atomic():
            source.save()
    except IntegrityError as exc:
        raise Conflict("A data source with this name already exists.", code="data_source_name_taken") from exc
    audit.record("data_source.update", target=source, request=request, changes={"before": before, "after": after})
    job = None
    if connection_changed and source.status != "disabled":
        job = start_test(source, user_id=user_id, request=None, audit_event=False)
    return source, job


def usage(source: DataSource) -> list[str]:
    used: list[str] = []
    for fn in _usage_checkers:
        used.extend(fn(source.id))
    return used


def delete_source(source: DataSource, *, request=None) -> None:
    users = usage(source)
    if users:
        raise SourceInUse(extra={"used_by": users[:20]})
    cred_id = source.credential_id
    audit.record("data_source.delete", target=source, request=request, metadata={"name": source.name})
    SourceImport.objects.filter(data_source_id=source.id).delete()
    source.delete()
    if cred_id:
        Credential.objects.filter(id=cred_id).delete()


def source_payload(source: DataSource) -> dict[str, Any]:
    """API shape. The credential is write-only: only `has_password` is exposed."""
    return {
        "id": str(source.id),
        "name": source.name,
        "type": source.type,
        "config": source.config,
        "username": source.username,
        "has_password": source.credential_id is not None,
        "status": source.status,
        "verified_readonly": source.verified_readonly,
        "resolved_ip": source.resolved_ip,
        "last_tested_at": source.last_tested_at.isoformat() if source.last_tested_at else None,
        "last_test_result": source.last_test_result,
        "last_error": source.last_error,
        "last_job_id": str(source.last_job_id) if source.last_job_id else None,
        "introspected_at": source.introspected_at.isoformat() if source.introspected_at else None,
        "created_by": str(source.created_by) if source.created_by else None,
        "created_at": source.created_at.isoformat(),
        "updated_at": source.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------------------------------
# jobs (enqueue)
# ---------------------------------------------------------------------------------------------------
def start_test(source: DataSource, *, user_id=None, request=None, audit_event: bool = True):
    from .tasks import test_data_source

    if source.status == "disabled":
        raise Conflict("The data source is disabled.", code="data_source_disabled")
    job = jobs.create_job("connector_test", user_id=user_id, message="Queued")
    DataSource.objects.filter(id=source.id).update(last_job_id=job.id)
    source.last_job_id = job.id
    if audit_event:
        audit.record("data_source.test.requested", target=source, request=request, metadata={"job_id": str(job.id)})
    kwargs = _job_kwargs(source, job, user_id)
    transaction.on_commit(lambda: test_data_source.delay(**kwargs))
    return job


def _require_ready(source: DataSource) -> None:
    if source.status != "active" or not source.verified_readonly:
        raise SourceNotReady()


def start_introspect(source: DataSource, *, user_id=None, request=None):
    from .tasks import introspect_data_source

    _require_ready(source)
    job = jobs.create_job("connector_introspect", user_id=user_id, message="Queued")
    audit.record("data_source.introspect", target=source, request=request, metadata={"job_id": str(job.id)})
    kwargs = _job_kwargs(source, job, user_id)
    transaction.on_commit(lambda: introspect_data_source.delay(**kwargs))
    return job


def _previous_import_dataset(source: DataSource, schema: str, table: str) -> UUID | None:
    prev = (SourceImport.objects.filter(data_source_id=source.id, schema_name=schema, table_name=table,
                                        status="succeeded", dataset_id__isnull=False)
            .order_by("-created_at").first())
    return prev.dataset_id if prev else None


def split_qualified_table(source: DataSource, table: str) -> tuple[str | None, str]:
    """`schema.table` (as the catalog browser lists it) -> (schema, table); anything else -> (None, table).

    A table name may itself contain a dot, so the name is only split when the stored catalog lists exactly that
    schema + table, or (no catalog yet) when the prefix is the source's configured schema.
    """
    prefix, dot, rest = table.partition(".")
    if not dot or not prefix or not rest:
        return None, table
    catalog = (source.catalog or {}).get("schemas") or []
    for s in catalog:
        if s.get("name") == prefix and any(t.get("name") == rest for t in s.get("tables") or ()):
            return prefix, rest
    if not catalog and prefix == (source.config or {}).get("schema"):
        return prefix, rest
    return None, table


def start_import(source: DataSource, *, table: str, name: str, schema: str | None = None,
                 columns: Sequence[str] | None = None, classification: str | None = None, attest: bool = False,
                 mode: str = "full", cursor_column: str | None = None, dataset_id=None, user_id=None,
                 request=None):
    """Queue an import of `schema.table` into a dataset (new, or a new version of the previous import's)."""
    from apps.datasets import services as datasets

    from .tasks import import_table

    _require_ready(source)
    schema = schema or (source.config or {}).get("schema") or "public"
    if (source.config or {}).get("schema") and schema != source.config["schema"]:
        raise ValidationError({"schema": ["Only the configured schema can be imported from."]})
    classification = classification or "internal"
    if classification not in CLASSIFICATIONS:
        raise ValidationError({"classification": [f"Must be one of {', '.join(CLASSIFICATIONS)}."]})
    if classification in ATTESTED and attest is not True:
        from apps.datasets.exceptions import AttestationRequired

        raise AttestationRequired()
    if mode == "incremental" and not cursor_column:
        raise ValidationError({"cursor_column": ["Incremental imports need a cursor column."]})
    target = None
    if dataset_id:
        target = datasets.get_dataset(dataset_id).id  # 404 when unknown / other org
    else:
        prev = _previous_import_dataset(source, schema, table)
        if prev is not None:
            try:
                target = datasets.get_dataset(prev).id
            except NotFound:
                target = None
    last_cursor = ""
    if mode == "incremental" and target is not None:
        prev_imp = (SourceImport.objects.filter(data_source_id=source.id, schema_name=schema, table_name=table,
                                                status="succeeded", dataset_id=target, cursor_column=cursor_column)
                    .order_by("-created_at").first())
        last_cursor = prev_imp.cursor_value if prev_imp else ""
    job = jobs.create_job("connector_import", user_id=user_id, message="Queued")
    imp = SourceImport.objects.create(
        data_source=source, schema_name=schema, table_name=table, columns=list(columns) if columns else None,
        mode=mode, cursor_column=cursor_column or "", cursor_value=last_cursor, dataset_id=target,
        dataset_name=name.strip()[:200], classification=classification, job_id=job.id, created_by=user_id,
    )
    audit.record("data_source.import", target=source, request=request, metadata={
        "table": f"{schema}.{table}", "dataset_name": imp.dataset_name, "dataset_id": str(target) if target else None,
        "mode": mode, "classification": classification, "import_id": str(imp.id), "job_id": str(job.id),
    })
    kwargs = {**_job_kwargs(source, job, user_id), "import_id": str(imp.id)}
    transaction.on_commit(lambda: import_table.delay(**kwargs))
    return imp, job


# ---------------------------------------------------------------------------------------------------
# worker side
# ---------------------------------------------------------------------------------------------------
def build_connector(source: DataSource) -> BaseConnector:
    """Decrypt the credential and build the connector (call only inside a worker task)."""
    cls = connector_class(source.type)
    return cls(source.config, username=source.username, password=_reveal_password(source))


def _error_dict(exc: ConnectorError) -> dict[str, str]:
    code = exc.get_codes() if isinstance(exc.get_codes(), str) else "connector_error"
    return {"code": code, "message": str(exc.detail)}


def run_test(*, org_id, source_id, job_id) -> dict:
    org = UUID(str(org_id))
    with tenant_step(org):
        source = DataSource.objects.get(id=source_id)
        jobs.update(job_id, status="running", progress=10, message="Connecting")
        connector = build_connector(source)
    result = connector.test()
    payload = result.as_dict()
    with tenant_step(org):
        source = DataSource.objects.get(id=source_id)
        source.last_tested_at = timezone.now()
        source.last_test_result = payload
        source.resolved_ip = result.resolved_ip or None
        if result.ok:
            source.status = "active" if source.status != "disabled" else "disabled"
            source.verified_readonly = True
            source.last_error = None
        else:
            source.status = "error" if source.status != "disabled" else "disabled"
            source.verified_readonly = False
            source.last_error = {"code": result.code or "connector_error", "message": result.message}
        source.save()
        audit.record("data_source.test", target=source, metadata={
            "ok": result.ok, "code": result.code, "readonly": payload["readonly"], "resolved_ip": result.resolved_ip,
            "job_id": str(job_id),
        })
        if result.ok:
            jobs.finish(job_id, result=payload)
        else:
            jobs.fail(job_id, code=result.code or "connector_error", message=result.message, detail=payload)
    return payload


def catalog_payload(tables: list[TableInfo]) -> dict[str, Any]:
    schemas: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        schemas.setdefault(t.schema, []).append({
            # `row_estimate` is the catalog browser's name for `estimated_rows` (additive alias)
            "name": t.name, "kind": t.kind, "estimated_rows": t.estimated_rows, "row_estimate": t.estimated_rows,
            "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable} for c in t.columns],
        })
    from .postgres import MAX_TABLES

    return {
        "schemas": [{"name": s, "tables": ts} for s, ts in sorted(schemas.items())],
        "table_count": len(tables),
        "truncated": len(tables) >= MAX_TABLES,
    }


def run_introspect(*, org_id, source_id, job_id) -> dict | None:
    org = UUID(str(org_id))
    with tenant_step(org):
        source = DataSource.objects.get(id=source_id)
        jobs.update(job_id, status="running", progress=10, message="Reading the catalog")
        connector = build_connector(source)
    try:
        tables = connector.introspect()
    except ConnectorError as exc:
        err = _error_dict(exc)
        with tenant_step(org):
            DataSource.objects.filter(id=source_id).update(last_error=err)
            jobs.fail(job_id, code=err["code"], message=err["message"])
        return None
    catalog = catalog_payload(tables)
    with tenant_step(org):
        DataSource.objects.filter(id=source_id).update(catalog=catalog, introspected_at=timezone.now())
        jobs.finish(job_id, result=catalog)
    return catalog


def write_frame_csv(frame: pl.DataFrame, path: str) -> int:
    """Write `frame` as UTF-8 CSV in the formats the ingest type inference reads back losslessly. -> bytes."""
    exprs = []
    for name, dtype in frame.schema.items():
        col = pl.col(name)
        if isinstance(dtype, pl.Datetime):
            if dtype.time_zone:
                exprs.append(col.dt.convert_time_zone("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S%.fZ").alias(name))
            else:
                exprs.append(col.dt.strftime("%Y-%m-%d %H:%M:%S%.f").alias(name))
        elif dtype == pl.Date:
            exprs.append(col.dt.strftime("%Y-%m-%d").alias(name))
        elif dtype == pl.Boolean:
            exprs.append(pl.when(col).then(pl.lit("true")).when(~col).then(pl.lit("false"))
                         .otherwise(pl.lit(None, dtype=pl.String)).alias(name))
        elif isinstance(dtype, (pl.List, pl.Array, pl.Struct, pl.Object)):
            exprs.append(col.map_elements(to_text, return_dtype=pl.String).alias(name))
        elif dtype == pl.Null:
            exprs.append(col.cast(pl.String).alias(name))
    out = frame.with_columns(exprs) if exprs else frame
    out.write_csv(path, include_header=True, null_value="", quote_style="necessary")
    return os.path.getsize(path)


def _batch_to_frame(batch: Batch) -> pl.DataFrame:
    data = {c.name: [to_text(r[i]) for r in batch.rows] for i, c in enumerate(batch.columns)}
    return pl.DataFrame(data, schema={c.name: pl.String for c in batch.columns})


def read_frame(source_id, table: str, *, schema: str | None = None, columns: Sequence[str] | None = None,
               limit: int | None = None, max_rows: int | None = None, org_id=None) -> pl.DataFrame:
    """Read an external table into a text-typed Polars frame (pipelines `source.connector`).

    The source must be verified; the read-only check is repeated on the live session. `limit` stops early
    (preview); `max_rows` fails with `import_too_large` when the table is bigger.
    """
    with tenant_step(org_id or require_org_id()):
        source = get_source(source_id)
        _require_ready(source)
        connector = build_connector(source)
    frames: list[pl.DataFrame] = []
    taken = 0
    batch_rows = min(IMPORT_BATCH_ROWS, limit) if limit else IMPORT_BATCH_ROWS
    gen = connector.read_batches(table, columns, batch_rows, schema=schema, max_rows=None if limit else max_rows)
    try:
        for batch in gen:
            frame = _batch_to_frame(batch)
            if limit is not None and taken + frame.height > limit:
                frame = frame.head(limit - taken)
            frames.append(frame)
            taken += frame.height
            if limit is not None and taken >= limit:
                break
    finally:
        gen.close()
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="vertical")


def _max_upload_bytes() -> int:
    return int(settings.MAX_UPLOAD_BYTES)


def _fail_import(org: UUID, import_id, job_id, code: str, message: str) -> None:
    with tenant_step(org):
        SourceImport.objects.filter(id=import_id).update(status="failed", error={"code": code, "message": message},
                                                          finished_at=timezone.now())
        imp = SourceImport.objects.filter(id=import_id).select_related("data_source").first()
        jobs.publish(job_id, "log", {"stage": "failed", "code": code, "message": message})
        jobs.fail(job_id, code=code, message=message)
        if imp is not None:
            audit.record("data_source.import.failed", target=imp.data_source,
                         metadata={"code": code, "message": message, "import_id": str(import_id)})
            _notify_import(imp, ok=False, code=code, message=message)


def _previous_rows_frame(dataset_id) -> pl.DataFrame | None:
    """The dataset's current rows, with columns renamed back to the external (source) names."""
    from apps.datasets import services as datasets

    try:
        dataset = datasets.get_dataset(dataset_id)
        version = datasets.require_ready_version(dataset)
    except Exception:  # noqa: BLE001 - no ready previous version: import as a full table
        return None
    columns = datasets.version_columns(version)
    frame = datasets.load_version_frame(version)
    return frame.rename({c["name"]: c["source_name"] or c["name"] for c in columns})


def run_import(*, org_id, source_id, import_id, job_id, actor_id=None) -> dict | None:
    """Stream the external table in batches into a CSV, then hand it to the standard upload/ingest path."""
    from apps.datasets import services as datasets
    from apps.lineage import services as lineage

    org = UUID(str(org_id))
    workdir = tempfile.mkdtemp(prefix="nexora_import_")
    try:
        with tenant_step(org):
            imp = SourceImport.objects.select_related("data_source").get(id=import_id)
            source = imp.data_source
            _require_ready(source)
            imp.status = "running"
            imp.save(update_fields=["status", "updated_at"])
            jobs.update(job_id, status="running", progress=5, message="Connecting")
            connector = build_connector(source)
            previous = None
            if imp.mode == "incremental" and imp.dataset_id and imp.cursor_value:
                previous = _previous_rows_frame(imp.dataset_id)
        after = imp.cursor_value if (imp.mode == "incremental" and previous is not None) else None
        path = os.path.join(workdir, f"{imp.table_name}.csv")
        limit_bytes = _max_upload_bytes()
        rows = 0
        last_cursor = None
        header: list[str] | None = None
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            for batch in connector.read_batches(
                imp.table_name, imp.columns, IMPORT_BATCH_ROWS, schema=imp.schema_name,
                cursor_column=imp.cursor_column or None, after=after,
                max_rows=int(getattr(settings, "DATASET_MAX_ROWS", 50_000_000)),
            ):
                if header is None:
                    header = [c.name for c in batch.columns]
                    writer.writerow(header)
                    cursor_idx = header.index(imp.cursor_column) if imp.cursor_column in header else None
                for row in batch.rows:
                    writer.writerow(["" if v is None else to_text(v) for v in row])
                rows += len(batch.rows)
                if batch.rows and cursor_idx is not None:
                    last_cursor = batch.rows[-1][cursor_idx]
                fh.flush()
                if os.path.getsize(path) > limit_bytes:
                    raise ImportTooLarge(f"The table is larger than the {limit_bytes} byte import limit.")
                with tenant_step(org):
                    jobs.update(job_id, status="running", progress=min(70, 10 + rows // 10_000),
                                message=f"Read {rows} rows")
        if previous is not None and header is not None:
            merged = os.path.join(workdir, f"{imp.table_name}_merged.csv")
            new_rows = pl.read_csv(path, infer_schema=False) if rows else pl.DataFrame(
                schema={h: pl.String for h in header})
            write_frame_csv(pl.concat([_textify(previous), new_rows], how="diagonal_relaxed"), merged)
            path = merged
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=80, message="Handing the rows to ingestion")
            imp = SourceImport.objects.select_related("data_source").get(id=import_id)
            with open(path, "rb") as fh:
                upload = datasets.create_upload(File(fh, name=f"{imp.table_name}.csv"), user_id=actor_id)
            dataset = None
            if imp.dataset_id:
                try:
                    dataset = datasets.get_dataset(imp.dataset_id)
                except NotFound:
                    dataset = None
            if dataset is not None:
                ingest_job = datasets.add_version(dataset, upload_id=upload.id, user_id=actor_id)
            else:
                dataset, ingest_job = datasets.create_dataset(
                    name=imp.dataset_name, upload_id=upload.id, classification=imp.classification,
                    attest=imp.classification in ATTESTED, user_id=actor_id,
                    description=f"Imported from {imp.data_source.name}: {imp.schema_name}.{imp.table_name}",
                )
            imp.dataset_id = dataset.id
            imp.ingest_job_id = ingest_job.id
            imp.rows = rows
            imp.status = "succeeded"
            imp.finished_at = timezone.now()
            if imp.cursor_column and last_cursor is not None:
                imp.cursor_value = to_text(last_cursor) or ""
            imp.save()
            lineage.record("data_source", imp.data_source_id, "dataset", dataset.id, run_id=imp.id, label="imported",
                           transform={"table": f"{imp.schema_name}.{imp.table_name}", "rows": rows, "mode": imp.mode})
            result = {
                "import_id": str(imp.id), "dataset_id": str(dataset.id), "rows": rows, "mode": imp.mode,
                "ingest_job_id": str(ingest_job.id),
                "table": f"{imp.schema_name}.{imp.table_name}",
            }
            audit.record("data_source.import.succeeded", target=imp.data_source, metadata=result)
            jobs.finish(job_id, result=result)
            _notify_import(imp, ok=True, dataset_id=dataset.id)
        return result
    except ConnectorError as exc:
        err = _error_dict(exc)
        _fail_import(org, import_id, job_id, err["code"], err["message"])
    except ValidationError as exc:
        _fail_import(org, import_id, job_id, "import_rejected", str(exc.detail)[:300])
    except Exception as exc:  # noqa: BLE001 - jobs carry stable codes, never internals
        code = exc.get_codes() if hasattr(exc, "get_codes") and isinstance(exc.get_codes(), str) else "import_failed"
        log.exception("connector import failed import=%s", import_id)
        _fail_import(org, import_id, job_id, code, "The import failed." if code == "import_failed" else str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return None


def _notify_import(imp: SourceImport, *, ok: bool, dataset_id=None, code: str = "", message: str = "") -> None:
    """In-app notification to the user who started the import (inside the import's tenant step)."""
    from apps.datasets import services as datasets
    from apps.datasets.notifications import IMPORT_FAILED, IMPORT_FINISHED

    table = f"{imp.schema_name}.{imp.table_name}"
    if ok:
        datasets.notify_users(
            [imp.created_by], IMPORT_FINISHED, f"Import of {table} finished",
            f"{imp.rows or 0:,} rows read from {imp.data_source.name} into '{imp.dataset_name}'. "
            "The dataset is being profiled and quality-checked.",
            datasets.org_link(f"data/datasets/{dataset_id}") if dataset_id else datasets.org_link("data/sources"))
    else:
        datasets.notify_users([imp.created_by], IMPORT_FAILED, f"Import of {table} failed", f"{message} ({code})",
                              datasets.org_link("data/sources"))


def _textify(frame: pl.DataFrame) -> pl.DataFrame:
    """Render a typed frame as text columns in the canonical CSV formats (temp CSV round-trip)."""
    if frame.height == 0:
        return pl.DataFrame(schema={name: pl.String for name in frame.columns})
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        write_frame_csv(frame, tmp)
        return pl.read_csv(tmp, infer_schema=False)
    finally:
        os.remove(tmp)
