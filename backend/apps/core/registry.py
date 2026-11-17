"""Neutral registries so lower layers (e.g. apps.ai) can query higher layers without importing them.

    # apps/datasets/apps.py -> ready():
    register_asset_classifier("dataset", lambda asset_id: Dataset.objects.get(id=asset_id).classification)

    # apps/ai policy gate:
    classification = classify_asset("dataset:<uuid>")   # -> "synthetic" | "public" | "internal" | "confidential"
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

_classifiers: dict[str, Callable[[UUID], str]] = {}


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
