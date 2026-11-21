"""Model artifact storage (CONTRACT "Safety rules for ML").

* Written ONLY through `apps.core.storage` under `orgs/<org_id>/ml/...` with the sha256 of the exact bytes.
* Loaded only when the key is inside the CURRENT org's `orgs/<org_id>/ml/` prefix AND the bytes match the
  recorded sha256; anything else raises ArtifactRejected before any deserialization.
* A small in-process LRU (ML_MODEL_CACHE_SIZE, default 3) keyed by (org, key, sha256) avoids re-reading.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from apps.core import storage
from apps.core.tenancy.context import require_org_id

from .engine.bundle import ArtifactIntegrityError, ModelBundle, dumps, loads_verified, sha256
from .exceptions import ArtifactRejected, ArtifactUnavailable

log = logging.getLogger("nexora.ml")

_cache: OrderedDict[tuple, ModelBundle] = OrderedDict()
_lock = threading.Lock()
_STORAGE_ERRORS = (BotoCoreError, ClientError, OSError)


def cache_size() -> int:
    return max(int(getattr(settings, "ML_MODEL_CACHE_SIZE", 3) or 3), 1)


def experiment_key(experiment_id) -> str:
    return storage.org_key("ml", "experiments", f"{experiment_id}.joblib")


def model_key(model_id, version: int) -> str:
    return storage.org_key("ml", "models", str(model_id), f"{int(version)}.joblib")


def snapshot_key(snapshot_id) -> str:
    return storage.org_key("ml", "snapshots", f"{snapshot_id}.parquet")


def _check_prefix(key: str) -> UUID:
    org_id = require_org_id()
    try:
        owner = storage.key_org_id(key)
    except (ValueError, PermissionError) as exc:
        raise ArtifactRejected("The artifact key is not an organization key.") from exc
    if owner != org_id or not key.startswith(f"orgs/{org_id}/ml/"):
        raise ArtifactRejected("The artifact does not belong to this organization's ML store.")
    return org_id


def save_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    _check_prefix(key)
    try:
        storage.put_bytes(key, data, content_type=content_type)
    except _STORAGE_ERRORS as exc:
        raise ArtifactUnavailable("The artifact could not be written to storage.") from exc
    return sha256(data)


def save_bundle(key: str, bundle: ModelBundle) -> tuple[str, int]:
    data = dumps(bundle)
    return save_bytes(key, data), len(data)


def read_verified_bytes(key: str, expected_sha256: str) -> bytes:
    _check_prefix(key)
    try:
        data = storage.get_object(key)
    except _STORAGE_ERRORS as exc:
        raise ArtifactUnavailable() from exc
    if not expected_sha256 or sha256(data) != expected_sha256:
        log.error("ml: artifact %s failed its sha256 check; refusing to load it", key)
        raise ArtifactRejected()
    return data


def load_bundle(key: str, expected_sha256: str) -> ModelBundle:
    org_id = _check_prefix(key)
    cache_key = (org_id, key, expected_sha256)
    with _lock:
        hit = _cache.get(cache_key)
        if hit is not None:
            _cache.move_to_end(cache_key)
            return hit
    data = read_verified_bytes(key, expected_sha256)
    try:
        bundle = loads_verified(data, expected_sha256)
    except ArtifactIntegrityError as exc:
        raise ArtifactRejected(str(exc)) from exc
    with _lock:
        _cache[cache_key] = bundle
        _cache.move_to_end(cache_key)
        while len(_cache) > cache_size():
            _cache.popitem(last=False)
    return bundle


def clear_cache() -> None:
    with _lock:
        _cache.clear()
