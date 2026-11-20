"""Local embeddings: never downloads a model in tests (SentenceTransformer is stubbed)."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from apps.ai.embeddings import (
    FakeEmbedder,
    LocalEmbedder,
    cosine,
    embed_query,
    get_embedder,
    override_embedder,
)


def test_fake_embedder_is_deterministic_and_normalized():
    e = FakeEmbedder(dim=768)
    a = e.embed_queries(["top 5 region by revenue <period>"])
    b = FakeEmbedder(dim=768).embed_queries(["top 5 region by revenue <period>"])
    assert a.shape == (1, 768)
    assert np.allclose(a, b)
    assert np.isclose(np.linalg.norm(a[0]), 1.0)


def test_fake_embedder_similarity_ordering():
    e = FakeEmbedder()
    base, para, other = e.embed_queries([
        "top <num> regions by revenue <period>",
        "top <num> region by revenue <period>",
        "average delivery time per warehouse",
    ])
    assert cosine(base, para) > 0.93
    assert cosine(base, other) < 0.5


def test_local_embedder_is_lazy_and_uses_e5_prefixes(monkeypatch, tmp_path):
    calls = {}

    class StubST:
        def __init__(self, name, cache_folder=None, device=None, local_files_only=None):
            calls["init"] = (name, cache_folder, device)
            calls["local_files_only"] = local_files_only

        def get_sentence_embedding_dimension(self):
            return 4

        def encode(self, texts, batch_size, normalize_embeddings, convert_to_numpy, show_progress_bar):
            calls.setdefault("texts", []).extend(texts)
            assert normalize_embeddings and convert_to_numpy and not show_progress_bar
            return np.array([[3.0, 4.0, 0.0, 0.0]] * len(texts))

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=StubST))
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    emb = LocalEmbedder("intfloat/multilingual-e5-base", dim=4, allow_download=True)
    assert not emb.loaded and "init" not in calls  # nothing loaded at construction
    q = emb.embed_queries(["revenue by region"])
    p = emb.embed_passages(["chunk one", "chunk two"])
    assert emb.loaded
    assert calls["init"] == ("intfloat/multilingual-e5-base", str(tmp_path), "cpu")
    assert calls["texts"] == ["query: revenue by region", "passage: chunk one", "passage: chunk two"]
    assert q.shape == (1, 4) and p.shape == (2, 4)
    assert np.allclose(np.linalg.norm(p, axis=1), 1.0)
    assert emb.embed_queries([]).shape == (0, 4)
    assert calls["local_files_only"] is False  # allow_download=True


def test_local_embedder_dimension_mismatch(monkeypatch):
    from django.core.exceptions import ImproperlyConfigured

    class StubST:
        def __init__(self, *a, **k):
            pass

        def get_sentence_embedding_dimension(self):
            return 1024

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=StubST))
    with pytest.raises(ImproperlyConfigured):
        LocalEmbedder("BAAI/bge-m3", dim=768, allow_download=True).embed_queries(["x"])


def test_override_embedder_and_embed_query():
    fake = FakeEmbedder(dim=16)
    import apps.ai.embeddings as mod

    before = mod._embedder
    with override_embedder(fake):
        assert get_embedder() is fake
        vec = embed_query("hello world")
        assert len(vec) == 16 and isinstance(vec[0], float)
    assert mod._embedder is before


def test_unsupported_provider(settings):
    from django.core.exceptions import ImproperlyConfigured

    import apps.ai.embeddings as mod

    settings.EMBEDDING_PROVIDER = "gemini"
    previous = mod._embedder
    mod._embedder = None
    try:
        with pytest.raises(ImproperlyConfigured):
            get_embedder()
    finally:
        mod._embedder = previous
