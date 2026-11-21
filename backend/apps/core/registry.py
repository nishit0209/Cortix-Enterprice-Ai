"""Neutral registries so lower layers (e.g. apps.ai, apps.governance) can query higher layers without
importing them.

    # apps/datasets/apps.py -> ready():
    register_asset_classifier("dataset", lambda asset_id: Dataset.objects.get(id=asset_id).classification)
    register_asset_exists("dataset", lambda asset_id: Dataset.objects.filter(id=asset_id).exists())
    register_dataset_schema_provider(lambda dataset_id: (tables, columns_by_table))

    # apps/ai policy gate:
    classification = classify_asset("dataset:<uuid>")   # -> "synthetic" | "public" | "internal" | "confidential"

All callables run inside the caller's tenant context (RLS applies), so they only ever see the active org.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from uuid import UUID

_classifiers: dict[str, Callable[[UUID], str]] = {}
_exists: dict[str, Callable[[UUID], bool]] = {}
# dataset_id -> (tables: logical -> physical "schema.table", columns_by_table: logical -> column names)
DatasetSchema = tuple[Mapping[str, str], Mapping[str, Iterable[str]]]
_dataset_schema_provider: list[Callable[[UUID], DatasetSchema]] = []


def register_asset_classifier(kind: str, fn: Callable[[UUID], str]) -> None:
    _classifiers[kind] = fn


def classify_asset(ref: str) -> str:
    """Resolve 'kind:uuid' to its classification. Unknown kinds/assets are treated as confidential."""
    try:
        kind, raw_id = ref.split(":", 1)
        fn = _classifiers[kind]
        return fn(UUID(raw_id)) or "confidential"
    except Exception:  # noqa: BLE001 - fail closed
        return "confidential"


def register_asset_exists(kind: str, fn: Callable[[UUID], bool]) -> None:
    """Register an existence check for an asset kind (evaluated inside the tenant context)."""
    _exists[kind] = fn


def asset_exists(kind: str, asset_id: UUID | str) -> bool | None:
    """True/False when the owning app registered a checker, None when existence cannot be verified."""
    fn = _exists.get(kind)
    if fn is None:
        return None
    try:
        return bool(fn(asset_id if isinstance(asset_id, UUID) else UUID(str(asset_id))))
    except (ValueError, TypeError):
        return False


# Policy preview sampler (registered by apps.bi): runs a small sample query under a GIVEN QueryPolicy.
#   fn(policy, *, actor_id, impersonated_user_id, limit) -> dict
_policy_sampler: list[Callable[..., dict]] = []


def register_policy_sampler(fn: Callable[..., dict]) -> None:
    _policy_sampler[:] = [fn]


def policy_sampler() -> Callable[..., dict] | None:
    return _policy_sampler[0] if _policy_sampler else None


def register_dataset_schema_provider(fn: Callable[[UUID], DatasetSchema]) -> None:
    """The datasets app registers how to resolve a dataset's warehouse tables and columns."""
    _dataset_schema_provider[:] = [fn]


def dataset_schema(dataset_id: UUID) -> DatasetSchema | None:
    if not _dataset_schema_provider:
        return None
    return _dataset_schema_provider[0](dataset_id)
