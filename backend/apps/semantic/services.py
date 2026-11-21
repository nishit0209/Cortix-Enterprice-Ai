"""Public service API of apps.semantic. Other apps import only from here.

    dataset_semantics(dataset_id) -> nexora.contracts.DatasetSemantics       (W2 contract; ensures a fresh model)
    ensure_model(dataset) / refresh_model(dataset, version=None, reset=False) -> SemanticModel
    model_payload(model) -> dict                                               (API shape)
    update_model(model, data, user_id=, request=) -> SemanticModel             (descriptions, synonyms, roles)
    create_metric / update_metric / delete_metric                              (expressions validated by sqlguard)
    on_version_published(...)                                                  (receiver of datasets' signal)

All functions run inside the caller's tenant context.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core import audit
from apps.datasets import services as datasets
from apps.datasets.models import Dataset, DatasetVersion
from apps.sqlguard.exceptions import GuardReject
from apps.sqlguard.services import validate_expression
from apps.warehouse import services as warehouse
from nexora.contracts import DatasetSemantics

from . import generation
from .models import MetricDefinition, SemanticColumn, SemanticModel, SemanticTable

log = logging.getLogger(__name__)

COLUMN_EDITABLE = ("display_name", "description", "synonyms", "role", "default_agg", "exposed_to_ai")
TABLE_EDITABLE = ("display_name", "description", "synonyms")
MAX_SYNONYMS = 20
MAX_SYNONYM_LEN = 100


# ------------------------------------------------------------------------------------------------ generation
def _ready_version(dataset: Dataset, version: DatasetVersion | None = None) -> DatasetVersion:
    return version if version is not None else datasets.require_ready_version(dataset)


@transaction.atomic
def refresh_model(dataset: Dataset, version: DatasetVersion | None = None, *, reset: bool = False,
                  user_id=None, request=None) -> SemanticModel:
    """(Re)generate the semantic model of `dataset` from `version` (default: the current ready version).

    User edits (`edited_fields`) on tables and columns that still exist are kept unless `reset=True`; columns
    that disappeared are removed; default metrics are regenerated; custom metrics are re-validated against
    the new columns and flagged `invalid` when they no longer apply.
    """
    version = _ready_version(dataset, version)
    cols = datasets.version_columns(version)
    logical = datasets.logical_table_name(dataset)
    specs = [generation.column_spec(c) for c in cols]

    model = SemanticModel.objects.select_for_update().filter(dataset_id=dataset.id).first()
    created = model is None
    if model is None:
        model = SemanticModel(dataset_id=dataset.id, name=dataset.name)
    model.dataset_version_id = version.id
    model.name = dataset.name
    if not model.description:
        model.description = dataset.description or ""
    model.status = "ready"
    model.last_error = None
    model.generated_at = timezone.now()
    model.schema_hash = generation.schema_hash({logical: ""}, {logical: [(c.name, c.data_type) for c in specs]})
    model.save()

    table = model.tables.first()
    if table is None:
        table = SemanticTable(model=model, logical_name=logical, display_name=dataset.name)
    table.logical_name = logical
    table.dataset_version_id = version.id
    if reset or "display_name" not in table.edited_fields:
        table.display_name = dataset.name[:200]
    if reset or "description" not in table.edited_fields:
        table.description = dataset.description or f"Dataset {dataset.name} (version {version.version})."
    if reset or "synonyms" not in table.edited_fields:
        table.synonyms = list(generation.synonyms_for(logical, dataset.name))
    if reset:
        table.edited_fields = []
    table.save()
    model.tables.exclude(id=table.id).delete()

    existing = {c.name: c for c in table.columns.all()}
    keep = set()
    for spec in specs:
        col = existing.get(spec.name) or SemanticColumn(table=table, name=spec.name)
        edited = set() if reset else set(col.edited_fields or ())
        col.ordinal, col.data_type, col.semantic_type = spec.ordinal, spec.data_type, spec.semantic_type
        col.is_pii = spec.is_pii if reset else (spec.is_pii or col.is_pii)
        generated = {
            "display_name": spec.display_name, "description": spec.description, "synonyms": list(spec.synonyms),
            "role": spec.role, "default_agg": spec.default_agg, "exposed_to_ai": True,
        }
        for field_name, value in generated.items():
            if field_name not in edited:
                setattr(col, field_name, value)
        if col.is_pii:
            col.role = "pii"  # detected PII can never be re-labelled away from pii by a refresh
        col.edited_fields = sorted(edited)
        col.save()
        keep.add(spec.name)
    table.columns.exclude(name__in=keep).delete()

    _sync_metrics(model, table, specs, dataset.name, reset=reset)
    audit.record("semantic.model.refresh" if not created else "semantic.model.create", target=model,
                 request=request, metadata={"dataset_id": str(dataset.id), "version": version.version,
                                            "columns": len(specs), "reset": reset,
                                            "actor": str(user_id) if user_id else None})
    return model


def _current_columns(table: SemanticTable) -> list[str]:
    return list(table.columns.values_list("name", flat=True))


def _sync_metrics(model: SemanticModel, table: SemanticTable, specs, dataset_name: str, *, reset: bool) -> None:
    columns = [s.name for s in specs]
    current = {m.key: m for m in model.metrics.all()}
    defaults = generation.default_metrics(table.logical_name, specs, dataset_name)
    default_keys = {d["key"] for d in defaults}
    for d in defaults:
        metric = current.get(d["key"])
        if metric is not None and not metric.is_default and not reset:
            continue  # a user-owned metric with the same key wins
        if metric is None:
            metric = MetricDefinition(model=model, key=d["key"], is_default=True)
        if reset:
            metric.is_active = True
        metric.is_default = True
        metric.table_name = d["table"]
        metric.name, metric.expression = d["name"], d["expression"]
        metric.description, metric.synonyms = d["description"], list(d["synonyms"])
        metric.status, metric.error = "valid", ""
        metric.save()
    for key, metric in current.items():
        if metric.is_default and key not in default_keys:
            metric.delete()  # its measure column is gone
        elif not metric.is_default:
            _revalidate(metric, table.logical_name, columns)


def _revalidate(metric: MetricDefinition, table_name: str, columns: list[str]) -> None:
    try:
        metric.expression = validate_expression(metric.expression, columns)
        metric.status, metric.error = "valid", ""
    except GuardReject as rej:
        metric.status, metric.error = "invalid", rej.message[:300]
    metric.table_name = table_name
    metric.save(update_fields=["expression", "status", "error", "table_name", "updated_at"])


def ensure_model(dataset: Dataset) -> SemanticModel:
    """The dataset's model, (re)generated when missing or built from an older version."""
    version = datasets.require_ready_version(dataset)
    model = SemanticModel.objects.filter(dataset_id=dataset.id).first()
    if model is None or model.dataset_version_id != version.id or not model.tables.exists():
        model = refresh_model(dataset, version)
    return model


def on_version_published(sender=None, *, org_id=None, dataset_id=None, version_id=None, **kwargs) -> None:
    """Receiver of apps.datasets.signals.dataset_version_published: regenerate the model for the new version."""
    dataset = Dataset.objects.filter(id=dataset_id).first()
    version = DatasetVersion.objects.filter(id=version_id, status="ready").first()
    if dataset is None or version is None:
        return
    try:
        with transaction.atomic():
            refresh_model(dataset, version)
    except Exception as exc:  # noqa: BLE001 - recorded on the model, never fails the ingestion
        log.exception("semantic refresh failed dataset=%s", dataset_id)
        with transaction.atomic():
            SemanticModel.objects.filter(dataset_id=dataset_id).update(
                status="failed", last_error={"code": "semantic_refresh_failed", "message": type(exc).__name__}
            )


# ------------------------------------------------------------------------------------------------ contract
def _column_meta(c: SemanticColumn) -> dict:
    return {
        "name": c.name, "type": c.data_type, "semantic_type": c.semantic_type, "role": c.role,
        "description": c.description, "synonyms": list(c.synonyms or ()), "is_pii": c.is_pii,
        "display_name": c.display_name, "default_agg": c.default_agg, "exposed_to_ai": c.exposed_to_ai,
    }


def _metric_meta(m: MetricDefinition) -> dict:
    return {"key": m.key, "name": m.name, "expression": m.expression, "description": m.description,
            "synonyms": list(m.synonyms or ()), "table": m.table_name}


MAX_KNOWN_VALUES = 50


def known_values(stats: dict | None, *, is_pii: bool) -> tuple[list[str], bool] | None:
    """(values, complete) from a column profile's `top_values` (text values only), or None.

    PII columns never yield values (the profiler stores none for them either). `complete` is True when every
    distinct value is listed. Used for `DatasetSemantics.columns_meta[*]["values"]` (local only: the LLM schema card
    never copies this key, so no VALUES payload reaches Gemini)."""
    if is_pii or not stats:
        return None
    top = stats.get("top_values") or []
    values = [str(t["value"]) for t in top if isinstance(t, dict) and t.get("value") not in (None, "")]
    if not values:
        return None
    values = values[:MAX_KNOWN_VALUES]
    distinct = stats.get("distinct")
    return values, isinstance(distinct, int) and distinct <= len(values)


def profiled_values(version_id) -> dict[str, tuple[list[str], bool]]:
    from apps.datasets.models import DatasetColumn

    out = {}
    for name, stats, is_pii in DatasetColumn.objects.filter(version_id=version_id).values_list("name", "stats", "is_pii"):
        found = known_values(stats, is_pii=is_pii)
        if found:
            out[name] = found
    return out


def dataset_semantics(dataset_id: UUID | str) -> DatasetSemantics:
    """CONTRACT (nexora.contracts.DatasetSemantics): logical->physical tables, ALL columns, column metadata,
    active valid metrics and the schema hash of the dataset's current version. Policy filtering is the
    caller's job (governance.effective_policy). Raises NotFound / DatasetNotReady."""
    dataset = datasets.get_dataset(dataset_id)
    model = ensure_model(dataset)
    version = DatasetVersion.objects.get(id=model.dataset_version_id)
    table = model.tables.get()
    columns = list(table.columns.order_by("ordinal"))
    metrics = model.metrics.filter(is_active=True, status="valid").order_by("created_at")
    known = profiled_values(version.id)
    metas = []
    for c in columns:
        meta = _column_meta(c)
        if c.role == "dimension" and not c.is_pii and c.name in known:
            meta["values"], meta["values_complete"] = known[c.name]
        metas.append(meta)
    return DatasetSemantics(
        dataset_id=dataset.id,
        dataset_version_id=version.id,
        dataset_name=dataset.name,
        classification=dataset.classification,
        tables={table.logical_name: warehouse.physical_table(version)},
        columns_by_table={table.logical_name: tuple(c.name for c in columns)},
        columns_meta={table.logical_name: tuple(metas)},
        metrics=tuple(_metric_meta(m) for m in metrics),
        schema_hash=model.schema_hash,
    )


# ------------------------------------------------------------------------------------------------ API
def model_payload(model: SemanticModel) -> dict:
    tables = []
    for t in model.tables.all().order_by("logical_name"):
        tables.append({
            "name": t.logical_name, "display_name": t.display_name, "description": t.description,
            "synonyms": list(t.synonyms or ()), "edited_fields": list(t.edited_fields or ()),
            "columns": [{**_column_meta(c), "data_type": c.data_type, "ordinal": c.ordinal,
                         "edited_fields": list(c.edited_fields or ())} for c in t.columns.order_by("ordinal")],
        })
    version = DatasetVersion.objects.filter(id=model.dataset_version_id).first()
    return {
        "id": str(model.id), "dataset_id": str(model.dataset_id),
        "dataset_version_id": str(model.dataset_version_id) if model.dataset_version_id else None,
        "version": version.version if version else None, "name": model.name, "description": model.description,
        "status": model.status, "last_error": model.last_error, "schema_hash": model.schema_hash,
        "generated_at": model.generated_at.isoformat() if model.generated_at else None,
        "updated_at": model.updated_at.isoformat() if model.updated_at else None,
        "tables": tables,
        "metrics": [metric_payload(m) for m in model.metrics.order_by("created_at")],
    }


def metric_payload(m: MetricDefinition) -> dict:
    return {
        "id": str(m.id), **_metric_meta(m), "is_default": m.is_default, "is_active": m.is_active,
        "status": m.status, "error": m.error or None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


def clean_synonyms(values: Any, field: str = "synonyms") -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValidationError({field: ["Must be a list of strings."]})
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValidationError({field: ["Must be a list of strings."]})
        v = " ".join(v.split()).lower()
        if not v:
            continue
        if len(v) > MAX_SYNONYM_LEN:
            raise ValidationError({field: [f"Each synonym must be at most {MAX_SYNONYM_LEN} characters."]})
        if v not in out:
            out.append(v)
    if len(out) > MAX_SYNONYMS:
        raise ValidationError({field: [f"At most {MAX_SYNONYMS} synonyms."]})
    return out


def _apply(obj, field: str, value, before: dict, after: dict) -> None:
    current = getattr(obj, field)
    if current != value:
        before[field], after[field] = current, value
        setattr(obj, field, value)
    edited = set(obj.edited_fields or ())
    edited.add(field)
    obj.edited_fields = sorted(edited)


@transaction.atomic
def update_model(model: SemanticModel, data: Mapping[str, Any], *, user_id=None, request=None) -> SemanticModel:
    """PATCH semantics. `data` = {description?, tables?: [{name, display_name?, description?, synonyms?}],
    columns?: [{name, table?, display_name?, description?, synonyms?, role?, default_agg?, exposed_to_ai?}]}.
    Edited fields survive refreshes. A detected PII column stays pii."""
    changes: dict[str, Any] = {"before": {}, "after": {}}
    if "description" in data:
        if model.description != data["description"]:
            changes["before"]["description"], changes["after"]["description"] = model.description, data["description"]
        model.description = data["description"]
    tables = {t.logical_name: t for t in model.tables.all()}
    default_table = next(iter(tables)) if len(tables) == 1 else None
    for item in data.get("tables") or ():
        table = tables.get(item["name"])
        if table is None:
            raise ValidationError({"tables": [f"Unknown table {item['name']!r}."]})
        before, after = {}, {}
        for field in TABLE_EDITABLE:
            if field in item:
                value = clean_synonyms(item[field]) if field == "synonyms" else item[field]
                _apply(table, field, value, before, after)
        table.save()
        if after:
            changes["before"][f"table:{table.logical_name}"] = before
            changes["after"][f"table:{table.logical_name}"] = after
    for item in data.get("columns") or ():
        table = tables.get(item.get("table") or default_table or "")
        if table is None:
            raise ValidationError({"columns": ["Each column needs a valid 'table'."]})
        col = table.columns.filter(name=item["name"]).first()
        if col is None:
            raise ValidationError({"columns": [f"Unknown column {item['name']!r}."]})
        before, after = {}, {}
        for field in COLUMN_EDITABLE:
            if field not in item:
                continue
            value = item[field]
            if field == "synonyms":
                value = clean_synonyms(value)
            if field == "role":
                if col.is_pii and value != "pii":
                    raise ValidationError({"columns": [f"{col.name!r} contains personal data; its role stays 'pii'."]})
                if value == "pii":
                    col.is_pii = True
            if field == "default_agg" and value is not None and col.role not in ("measure", "id") and \
                    item.get("role", col.role) not in ("measure", "id"):
                raise ValidationError({"columns": [f"default_agg only applies to measures and ids ({col.name!r})."]})
            _apply(col, field, value, before, after)
        col.save()
        if after:
            changes["before"][f"column:{col.name}"] = before
            changes["after"][f"column:{col.name}"] = after
    model.updated_by = user_id
    model.save()
    audit.record("semantic.model.update", target=model, request=request, changes=changes)
    return model


def _model_columns(model: SemanticModel) -> tuple[str, list[str]]:
    table = model.tables.get()
    return table.logical_name, _current_columns(table)


def create_metric(model: SemanticModel, data: Mapping[str, Any], *, user_id=None, request=None) -> MetricDefinition:
    table_name, columns = _model_columns(model)
    try:
        expression = validate_expression(data["expression"], columns)
    except GuardReject as rej:
        raise ValidationError({"expression": [f"{rej.message} ({rej.code})"]}) from rej
    key = generation.metric_key(data.get("key") or data["name"])
    metric = MetricDefinition(
        model=model, table_name=table_name, key=key, name=data["name"].strip(), expression=expression,
        description=data.get("description", ""), synonyms=clean_synonyms(data.get("synonyms", [])),
        created_by=user_id,
    )
    try:
        with transaction.atomic():
            metric.save()
    except IntegrityError as exc:
        raise ValidationError({"key": [f"A metric with key {key!r} already exists."]}) from exc
    audit.record("semantic.metric.create", target=metric, request=request,
                 metadata={"key": key, "expression": expression})
    return metric


def update_metric(metric: MetricDefinition, data: Mapping[str, Any], *, request=None) -> MetricDefinition:
    before, after = {}, {}
    if "expression" in data:
        _, columns = _model_columns(metric.model)
        try:
            value = validate_expression(data["expression"], columns)
        except GuardReject as rej:
            raise ValidationError({"expression": [f"{rej.message} ({rej.code})"]}) from rej
        if value != metric.expression:
            before["expression"], after["expression"] = metric.expression, value
        metric.expression, metric.status, metric.error = value, "valid", ""
    for field in ("name", "description", "is_active"):
        if field in data and getattr(metric, field) != data[field]:
            before[field], after[field] = getattr(metric, field), data[field]
            setattr(metric, field, data[field])
    if "synonyms" in data:
        value = clean_synonyms(data["synonyms"])
        if value != list(metric.synonyms or ()):
            before["synonyms"], after["synonyms"] = list(metric.synonyms or ()), value
        metric.synonyms = value
    if after and metric.is_default and set(after) - {"is_active"}:
        metric.is_default = False  # edited defaults become user-owned and survive refreshes
    metric.save()
    audit.record("semantic.metric.update", target=metric, request=request, changes={"before": before, "after": after})
    return metric


def delete_metric(metric: MetricDefinition, *, request=None) -> None:
    """Custom metrics are deleted; generated defaults are deactivated so a refresh does not bring them back."""
    audit.record("semantic.metric.delete", target=metric, request=request,
                 metadata={"key": metric.key, "default": metric.is_default})
    if metric.is_default:
        metric.is_active = False
        metric.save(update_fields=["is_active", "updated_at"])
    else:
        metric.delete()
