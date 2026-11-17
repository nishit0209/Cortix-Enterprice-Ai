"""Local embeddings (default provider; no Gemini call) — AI_ARCHITECTURE.md §7.3.

* `LocalEmbedder`: sentence-transformers model `settings.EMBEDDING_MODEL` (default intfloat/multilingual-e5-base,
  768-d), loaded LAZILY on first use (cache dir from HF_HOME / SENTENCE_TRANSFORMERS_HOME), e5 prefixes
  "query: " / "passage: ", L2-normalised float32 vectors, batched encoding.
* `FakeEmbedder`: deterministic hashing embedder for tests (never downloads anything). Lexically similar
  texts get high cosine similarity, unrelated texts get low similarity.

Use `get_embedder()`; tests swap the instance with `override_embedder(FakeEmbedder())`.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class Embedder(Protocol):
    dim: int

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...


def _normalize(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def cosine(a: Sequence[float] | np.ndarray, b: Sequence[float] | np.ndarray) -> float:
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


class LocalEmbedder:
    def __init__(self, model_name: str | None = None, *, dim: int | None = None, cache_dir: str | None = None,
                 device: str = "cpu", batch_size: int = 32):
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.dim = int(dim or settings.EMBEDDING_DIM)
        self.cache_dir = cache_dir or os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME") or None
        self.device = device
        self.batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer

                    model = SentenceTransformer(self.model_name, cache_folder=self.cache_dir, device=self.device)
                    got = model.get_sentence_embedding_dimension()
                    if got is not None and int(got) != self.dim:
                        raise ImproperlyConfigured(
                            f"EMBEDDING_MODEL {self.model_name} has dimension {got}, EMBEDDING_DIM is {self.dim}")
                    self._model = model
        return self._model

    def _encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        model = self._load()
        vecs = model.encode([prefix + (t or "") for t in texts], batch_size=self.batch_size,
                            normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
        return _normalize(vecs)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(list(texts), QUERY_PREFIX)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(list(texts), PASSAGE_PREFIX)


_TOKEN_RE = re.compile(r"[\w<>]+", re.U)


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


class FakeEmbedder:
    """Deterministic, dependency-free embedder (feature hashing of words + character trigrams)."""

    def __init__(self, dim: int | None = None):
        self.dim = int(dim or getattr(settings, "EMBEDDING_DIM", 768))
        self.calls = 0

    def _bucket(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % self.dim
        sign = 1.0 if h[4] & 1 else -1.0
        return idx, sign

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = [_stem(t) for t in _TOKEN_RE.findall((text or "").lower())]
        for tok in toks:
            i, s = self._bucket("w:" + tok)
            v[i] += 2.0 * s
            padded = f"#{tok}#"
            for j in range(len(padded) - 2):
                i, s = self._bucket("c:" + padded[j:j + 3])
                v[i] += 0.5 * s
        for a, b in zip(toks, toks[1:], strict=False):
            i, s = self._bucket(f"b:{a}_{b}")
            v[i] += 1.0 * s
        return v

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalize(np.stack([self._vec(t) for t in texts]))

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(list(texts))

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(list(texts))


_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                provider = str(getattr(settings, "EMBEDDING_PROVIDER", "local")).lower()
                if provider != "local":
                    raise ImproperlyConfigured(f"EMBEDDING_PROVIDER={provider!r} is not supported (use 'local')")
                _embedder = LocalEmbedder()
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    global _embedder
    _embedder = embedder


@contextmanager
def override_embedder(embedder: Embedder) -> Iterator[Embedder]:
    global _embedder
    previous = _embedder
    _embedder = embedder
    try:
        yield embedder
    finally:
        _embedder = previous


def embed_query(text: str) -> list[float]:
    return get_embedder().embed_queries([text])[0].astype(float).tolist()


def embed_passages(texts: Sequence[str]) -> np.ndarray:
    return get_embedder().embed_passages(texts)
