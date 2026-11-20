"""Known dimension values for the deterministic builder's filters ("revenue from Electronics", "online orders").

The datasets profiler stores the most frequent values of every non-PII column of a dataset version
(`DatasetColumn.stats["top_values"]`). `with_known_values(ctx, dataset_version_id)` copies them into the
`values` key of the matching dimension columns of `ctx.columns_meta`, which `apps.nl2sql.filters` turns into
`"col" = '<stored value>'` conditions.

* Local only: the values are used by the builder in this process. The LLM schema card never includes them
  (`prompting.build_schema_card` copies a fixed set of keys), so no VALUES payload is ever sent to Gemini.
* Fail closed / best effort: PII columns (no top values are profiled for them), masked columns and columns the
  user may not see are skipped; any lookup problem (no database, dataset not ready, old version) leaves the
  context unchanged.
* A dataset version is immutable, so its values are cached per (org, version) in process (bounded).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import replace
from uuid import UUID

from nexora.contracts import QuestionContext

log = logging.getLogger("nexora.nl2sql")

CACHE_MAX = 256
MAX_VALUES = 200
VALUE_KEYS = ("values", "top_values", "sample_values", "enum")

_cache: OrderedDict[tuple[UUID, UUID], dict[str, tuple[str, ...]]] = OrderedDict()
_lock = threading.Lock()


def _remember(key: tuple[UUID, UUID], values: dict[str, tuple[str, ...]]) -> None:
    with _lock:
        _cache[key] = values
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def profile_values(columns: list[dict]) -> dict[str, tuple[str, ...]]:
    """column name -> profiled top values (strings only; PII columns are never included). Pure."""
    out: dict[str, tuple[str, ...]] = {}
    for col in columns or ():
        if col.get("is_pii"):
            continue
        top = (col.get("stats") or {}).get("top_values") or []
        vals = [str(t["value"]).strip() for t in top
                if isinstance(t, dict) and isinstance(t.get("value"), str) and str(t["value"]).strip()]
        if vals:
            out[str(col.get("name"))] = tuple(dict.fromkeys(vals))[:MAX_VALUES]
    return out


def known_values(ctx: QuestionContext, dataset_version_id: UUID | None) -> dict[str, tuple[str, ...]]:
    """Profiled values of the dataset's CURRENT version (inside the tenant context). {} when unavailable or when
    `dataset_version_id` names another version (values of a different version could be wrong)."""
    if dataset_version_id is not None:
        with _lock:
            hit = _cache.get((ctx.org_id, dataset_version_id))
        if hit is not None:
            return hit
    from apps.datasets import services as datasets

    dataset = datasets.get_dataset(ctx.dataset_id)
    version = datasets.current_version(dataset)
    if version is None or getattr(version, "status", "ready") != "ready":
        return {}
    if dataset_version_id is not None and version.id != dataset_version_id:
        return {}
    values = profile_values(datasets.version_columns(version))
    _remember((ctx.org_id, version.id), values)
    return values


def apply_values(ctx: QuestionContext, values: dict[str, tuple[str, ...]]) -> QuestionContext:
    """A copy of ctx whose unmasked, non-PII columns without listed values get `values`. Pure."""
    if not values:
        return ctx
    from .builder import PII_SEMANTIC_TYPES

    changed = False
    columns_meta: dict[str, tuple[dict, ...]] = {}
    for table, metas in (ctx.columns_meta or {}).items():
        out = []
        for meta in metas:
            name = str(meta.get("name") or "")
            pii = bool(meta.get("is_pii")) or str(meta.get("semantic_type") or "").lower() in PII_SEMANTIC_TYPES
            if (name in values and not meta.get("masked") and not pii
                    and (table, name) not in (ctx.policy.column_masks or {})
                    and not any(meta.get(k) for k in VALUE_KEYS)):
                meta = {**meta, "values": list(values[name])}
                changed = True
            out.append(meta)
        columns_meta[table] = tuple(out)
    return replace(ctx, columns_meta=columns_meta) if changed else ctx


def with_known_values(ctx: QuestionContext, dataset_version_id: UUID | None = None) -> QuestionContext:
    """ctx enriched with profiled dimension values; ctx unchanged on any problem (never raises)."""
    if all(any(m.get(k) for k in VALUE_KEYS) for metas in (ctx.columns_meta or {}).values() for m in metas):
        return ctx
    from apps.core.tenancy.context import get_current_org_id

    if get_current_org_id() != ctx.org_id:
        return ctx  # offline tools / foreign context: never touch the database
    from django.db import transaction

    # Inside the caller's transaction a failing lookup must not poison it: run it in a savepoint.
    guard = transaction.atomic() if transaction.get_connection().in_atomic_block else nullcontext()
    try:
        with guard:
            values = known_values(ctx, dataset_version_id)
    except Exception as exc:  # noqa: BLE001 - an optimisation for the builder; never fail the question
        log.debug("nl2sql: dimension values unavailable (%s)", type(exc).__name__)
        return ctx
    return apply_values(ctx, values)
