"""In-memory replacement for apps.core.storage (MinIO) that keeps the tenant-prefix enforcement."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemoryObjectStorage:
    """`install(monkeypatch)` swaps put_object/put_bytes/get_object/download_to/delete_object/presigned_get.

    Every operation still runs apps.core.storage._check_key (orgs/<active org>/ prefix, fail-closed without a
    tenant), so cross-tenant key bugs surface exactly as they would against MinIO.
    """

    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    presigned: list[tuple[str, int, str | None]] = field(default_factory=list)
    _check: Any = None

    def install(self, monkeypatch) -> MemoryObjectStorage:
        from apps.core import storage

        self._check = storage._check_key
        for name in ("put_object", "put_bytes", "get_object", "download_to", "delete_object", "presigned_get"):
            monkeypatch.setattr(storage, name, getattr(self, name))
        monkeypatch.setattr(storage, "ensure_bucket", lambda: None)
        return self

    def _checked(self, key: str) -> None:
        if self._check is None:
            raise RuntimeError("MemoryObjectStorage.install() was not called")
        self._check(key)

    def put_object(self, key, fileobj, content_type="application/octet-stream"):
        self._checked(key)
        self.objects[key] = fileobj.read()
        self.content_types[key] = content_type

    def put_bytes(self, key, data, content_type="application/octet-stream"):
        self.put_object(key, io.BytesIO(bytes(data)), content_type)

    def get_object(self, key):
        self._checked(key)
        return self.objects[key]

    def download_to(self, key, path):
        self._checked(key)
        with open(path, "wb") as fh:
            fh.write(self.objects[key])

    def delete_object(self, key):
        self._checked(key)
        self.objects.pop(key, None)

    def presigned_get(self, key, expires_s=600, filename=None):
        self._checked(key)
        self.presigned.append((key, expires_s, filename))
        return f"https://storage.invalid/{key}?expires={expires_s}"

    def keys_for(self, org_id) -> list[str]:
        prefix = f"orgs/{org_id}/"
        return sorted(k for k in self.objects if k.startswith(prefix))
