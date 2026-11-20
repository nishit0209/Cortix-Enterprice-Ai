"""Fixtures for apps.documents tests: in-memory object storage and the deterministic FakeEmbedder."""

from __future__ import annotations

import pytest

from apps.ai.embeddings import FakeEmbedder
from apps.ai.services import override_embedder
from apps.core import storage


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, key, fileobj, content_type="application/octet-stream"):
        storage._check_key(key)
        self.objects[key] = fileobj.read()

    def download_to(self, key, path):
        storage._check_key(key)
        with open(path, "wb") as fh:
            fh.write(self.objects[key])

    def delete_object(self, key):
        storage._check_key(key)
        self.objects.pop(key, None)


@pytest.fixture
def fake_storage(monkeypatch):
    fake = FakeStorage()
    for name in ("put_object", "download_to", "delete_object"):
        monkeypatch.setattr(storage, name, getattr(fake, name))
    return fake


@pytest.fixture
def fake_embedder():
    with override_embedder(FakeEmbedder()) as emb:
        yield emb
