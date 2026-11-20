"""Local embeddings (default provider; no Gemini call) — AI_ARCHITECTURE.md §7.3.

* `LocalEmbedder`: sentence-transformers model `settings.EMBEDDING_MODEL` (default intfloat/multilingual-e5-base,
  768-d, about 1.1 GB), loaded LAZILY from the local cache (EMBEDDING_CACHE_DIR / SENTENCE_TRANSFORMERS_HOME /
  HF_HOME). It is fetched by `manage.py download_models` or by the `ai.reembed_pending` background job; request
  paths never download and raise `EmbeddingUnavailable` instead. Request paths never WAIT either: while the model
  is being downloaded or loaded by another thread they get EmbeddingUnavailable("model_downloading" /
  "model_loading") at once and degrade. e5 prefixes "query: " / "passage: ", L2-normalised float32 vectors,
  batched encoding.
* `FakeEmbedder`: deterministic hashing embedder for tests (never downloads anything). Lexically similar
  texts get high cosine similarity, unrelated texts get low similarity.

Use `get_embedder()`; tests swap the instance with `override_embedder(FakeEmbedder())`.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import NoReturn, Protocol

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


class EmbeddingUnavailable(RuntimeError):
    """The local embedding model cannot be used right now (not downloaded yet, offline, failed to load).

    Callers degrade: documents keep `embedding_pending` chunks and search full-text only; nl2sql skips the
    semantic cache. Nothing user-facing fails because of it."""

    def __init__(self, message: str, *, reason: str = "model_unavailable"):
        super().__init__(message)
        self.reason = reason


FAILURE_COOLDOWN_S = 300.0  # after a failed load, fail fast for this long instead of retrying on every call
_WEIGHT_SUFFIXES = (".safetensors", ".bin")


def model_cache_dir() -> str | None:
    """Where the model is cached: EMBEDDING_CACHE_DIR, else SENTENCE_TRANSFORMERS_HOME / HF_HOME, else the
    Hugging Face default (~/.cache/huggingface/hub)."""
    return (str(getattr(settings, "EMBEDDING_CACHE_DIR", "") or "") or os.environ.get("SENTENCE_TRANSFORMERS_HOME")
            or os.environ.get("HF_HOME") or None)


def auto_download_allowed() -> bool:
    """Background jobs (`ai.reembed_pending`) may download the model when EMBEDDING_AUTO_DOWNLOAD is true
    (default). Request paths and document processing never download."""
    return bool(getattr(settings, "EMBEDDING_AUTO_DOWNLOAD", True))


class LocalEmbedder:
    """sentence-transformers model loaded lazily from the LOCAL cache.

    `allow_download=False` (the default for the process-wide instance) means a model that is not cached yet raises
    EmbeddingUnavailable immediately instead of starting a 1.1 GB download inside a request. `download()` (used by
    `manage.py download_models` and the re-embed job) fetches it explicitly.

    Locking: the download (network + load) runs WITHOUT `_lock`, guarded by its own `_download_lock`, and only the
    hand-over of the finished model takes `_lock`. While a download runs `available` is False. Callers that may
    not download (requests, document processing) never block: if another thread holds `_lock` (it is loading the
    model from the cache) they get EmbeddingUnavailable(reason="model_loading") and degrade.
    """

    def __init__(self, model_name: str | None = None, *, dim: int | None = None, cache_dir: str | None = None,
                 device: str = "cpu", batch_size: int = 32, allow_download: bool = False,
                 clock=time.monotonic):
        self.model_name = model_name or settings.EMBEDDING_MODEL
        self.dim = int(dim or settings.EMBEDDING_DIM)
        self.cache_dir = cache_dir or model_cache_dir()
        self.device = device
        self.batch_size = batch_size
        self.allow_download = allow_download
        self._clock = clock
        self._model = None
        self._lock = threading.Lock()  # held only while loading from the local cache / installing a model
        self._download_lock = threading.Lock()  # one download at a time; request paths never take it
        self._downloading = False
        self._failed_until = 0.0
        self._last_error = ""

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def downloading(self) -> bool:
        return self._downloading

    def cached_path(self) -> str | None:
        """The local model directory (a directory name, or a COMPLETE Hugging Face cache snapshot), else None.

        A snapshot without a weights file yet (a download in progress, possibly in another process: Hugging Face
        creates `snapshots/<sha>/` before the large file arrives) does not count as cached."""
        if os.path.isdir(self.model_name):
            return os.path.abspath(self.model_name)
        try:
            from huggingface_hub import snapshot_download

            path = snapshot_download(repo_id=self.model_name, cache_dir=self.cache_dir, local_files_only=True)
        except Exception:  # noqa: BLE001 - LocalEntryNotFoundError, offline errors, bad cache: all "not cached"
            return None
        if not path or not os.path.isdir(path) or not _has_weights(path):
            return None
        return path

    def is_cached(self) -> bool:
        return self.cached_path() is not None

    @property
    def available(self) -> bool:
        """Can embeddings be computed now without a download? (cheap; never loads or downloads)."""
        if self._model is not None:
            return True
        if self._downloading or self._clock() < self._failed_until:
            return False
        return self.allow_download or self.is_cached()

    def status(self) -> dict:
        loaded = self._model is not None
        cooling = not loaded and self._clock() < self._failed_until
        downloading = not loaded and self._downloading
        cached = loaded or (not downloading and self.is_cached())
        return {"provider": "local", "model": self.model_name, "dim": self.dim, "loaded": loaded,
                "cached": cached, "cache_dir": self.cache_dir, "downloading": downloading,
                "available": loaded or (not cooling and not downloading and (cached or self.allow_download)),
                "last_error": self._last_error or None}

    def _fail(self, message: str, *, reason: str, error: str, cause: BaseException | None = None) -> NoReturn:
        self._last_error = error
        self._failed_until = self._clock() + FAILURE_COOLDOWN_S
        raise EmbeddingUnavailable(message, reason=reason) from cause

    def _construct(self, *, local_files_only: bool):
        """Build the SentenceTransformer (fetches missing files unless local_files_only). Takes no lock."""
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, cache_folder=self.cache_dir, device=self.device,
                                        local_files_only=local_files_only)
        except ImportError as exc:
            self._fail("sentence-transformers is not installed", reason="library_missing",
                       error="sentence-transformers is not installed", cause=exc)
        except Exception as exc:  # noqa: BLE001 - offline, corrupt cache, out of memory
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._fail(f"could not load {self.model_name}: {error}", reason="load_failed", error=error, cause=exc)
        got = model.get_sentence_embedding_dimension()
        if got is not None and int(got) != self.dim:
            raise ImproperlyConfigured(
                f"EMBEDDING_MODEL {self.model_name} has dimension {got}, EMBEDDING_DIM is {self.dim}")
        return model

    def _install(self, model) -> None:
        self._model = model
        self._failed_until = 0.0
        self._last_error = ""

    def _load(self, *, allow_download: bool | None = None):
        if self._model is not None:
            return self._model
        allow = self.allow_download if allow_download is None else allow_download
        if allow:
            return self._download_and_install()
        if self._downloading:
            raise EmbeddingUnavailable(f"{self.model_name} is being downloaded", reason="model_downloading")
        if not self._lock.acquire(blocking=False):  # another thread is loading it: degrade, never wait
            raise EmbeddingUnavailable(f"{self.model_name} is loading", reason="model_loading")
        try:
            if self._model is not None:
                return self._model
            if self._clock() < self._failed_until:
                raise EmbeddingUnavailable(f"embedding model unavailable: {self._last_error}",
                                           reason="model_unavailable")
            if not self.is_cached():
                self._fail(f"{self.model_name} is not downloaded yet", reason="model_not_downloaded",
                           error="model not downloaded (run `manage.py download_models`)")
            self._install(self._construct(local_files_only=True))
            return self._model
        finally:
            self._lock.release()

    def _download_and_install(self):
        """Download (when needed) and load OUTSIDE `_lock`; only installing the finished model takes it."""
        if not self._download_lock.acquire(blocking=False):
            raise EmbeddingUnavailable(f"{self.model_name} is being downloaded", reason="model_downloading")
        try:
            if self._model is not None:
                return self._model
            self._downloading = True
            try:
                model = self._construct(local_files_only=False)
            finally:
                self._downloading = False
            with self._lock:  # waits at most for a concurrent load from the cache, never for a download
                if self._model is None:
                    self._install(model)
            return self._model
        finally:
            self._download_lock.release()

    def download(self) -> str | None:
        """Fetch (or verify) the model files and load it. Returns the local snapshot path.
        Raises EmbeddingUnavailable(reason="model_downloading") while another thread is downloading it."""
        self._failed_until = 0.0
        self._load(allow_download=True)
        return self.cached_path()

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


def _has_weights(path: str) -> bool:
    """True when the snapshot holds a weights file (Hugging Face links it in only once it is fully downloaded)."""
    for _root, _dirs, files in os.walk(path):
        if any(f.endswith(_WEIGHT_SUFFIXES) for f in files):
            return True
    return False


class UnavailableEmbedder:
    """Test double for a missing model: every call raises EmbeddingUnavailable."""

    available = False

    def __init__(self, dim: int | None = None, reason: str = "model_not_downloaded"):
        self.dim = int(dim or getattr(settings, "EMBEDDING_DIM", 768))
        self.reason = reason
        self.calls = 0

    def status(self) -> dict:
        return {"provider": "fake", "model": "unavailable", "dim": self.dim, "loaded": False, "cached": False,
                "cache_dir": None, "available": False, "last_error": self.reason}

    def _fail(self, texts):
        self.calls += 1
        raise EmbeddingUnavailable("embedding model unavailable (test double)", reason=self.reason)

    embed_queries = _fail
    embed_passages = _fail


_TOKEN_RE = re.compile(r"[\w<>]+", re.U)


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


class FakeEmbedder:
    """Deterministic, dependency-free embedder (feature hashing of words + character trigrams)."""

    available = True

    def __init__(self, dim: int | None = None):
        self.dim = int(dim or getattr(settings, "EMBEDDING_DIM", 768))
        self.calls = 0

    def status(self) -> dict:
        return {"provider": "fake", "model": "hashing", "dim": self.dim, "loaded": True, "cached": True,
                "cache_dir": None, "available": True, "last_error": None}

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


def embedding_available() -> bool:
    """Cheap check (no load, no download): can a query be embedded now? Unknown embedders count as available."""
    try:
        return bool(getattr(get_embedder(), "available", True))
    except Exception:  # noqa: BLE001 - misconfigured provider -> unavailable, never an error
        return False


def embedder_status() -> dict:
    try:
        emb = get_embedder()
    except Exception as exc:  # noqa: BLE001
        return {"provider": str(getattr(settings, "EMBEDDING_PROVIDER", "local")), "available": False,
                "last_error": str(exc)[:200]}
    status = getattr(emb, "status", None)
    if callable(status):
        return status()
    return {"provider": type(emb).__name__, "available": True, "dim": getattr(emb, "dim", None)}


def embed_query(text: str) -> list[float]:
    """Raises EmbeddingUnavailable when the model is not available (callers degrade, never fail a request)."""
    try:
        return get_embedder().embed_queries([text])[0].astype(float).tolist()
    except EmbeddingUnavailable:
        raise
    except ImproperlyConfigured as exc:
        raise EmbeddingUnavailable(str(exc), reason="misconfigured") from exc


def embed_passages(texts: Sequence[str]) -> np.ndarray:
    return get_embedder().embed_passages(texts)
