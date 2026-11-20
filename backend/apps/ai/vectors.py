"""Vector storage that works with AND without pgvector (CONTRACT "Local runtime": AI/ML vector fallback).

* `EmbeddingField(dim)`: a model field whose column is `vector(dim)` when the effective backend is pgvector,
  otherwise `double precision[]` (plain PostgreSQL). Python values are float32 numpy arrays; lists, tuples and
  numpy arrays are accepted on input for both backends.
* `effective_backend(connection)`: `settings.VECTOR_BACKEND` = "pgvector" | "array" | "auto". "auto" means
  pgvector when the `vector` extension is INSTALLED in the database (the migrations install it when it is
  available), cached per physical connection.
* `nearest(queryset, field, query_vec, k, max_scan=20000)`: the k nearest rows by cosine distance, each with a
  `distance` attribute. pgvector: SQL `<=>` (HNSW index). array: at most `max_scan` candidate vectors of the
  (already org/dataset-filtered) queryset are loaded and ranked with numpy.
* Migration helpers: `ensure_vector_extension()` and `hnsw_index()` return reversible RunPython operations that
  create the extension / HNSW index ONLY when the backend is pgvector.

The column type actually present in the database wins over the setting at runtime (`field_backend`), so a
database migrated without pgvector keeps working if pgvector is installed later (and vice versa).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, connections, migrations, models, transaction
from django.db.models import F, FloatField, Func
from django.db.models.expressions import Expression

log = logging.getLogger("nexora.ai")

PGVECTOR = "pgvector"
ARRAY = "array"
AUTO = "auto"
BACKENDS = (PGVECTOR, ARRAY, AUTO)
DEFAULT_MAX_SCAN = 20_000

_ATTR = "_nexora_vector_backend"
_COLS_ATTR = "_nexora_vector_columns"


# ------------------------------------------------------------------------------------------- conversions
def to_array(value) -> np.ndarray | None:
    """list / tuple / numpy / pgvector text ('[1,2,3]') / postgres array text ('{1,2,3}') -> float32 1-d array."""
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        arr = value.astype(np.float32, copy=False)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[0] in "[{" and text[-1] in "]}":
            text = text[1:-1]
        parts = [p for p in text.split(",") if p.strip()]
        arr = np.asarray([float(p) for p in parts], dtype=np.float32)
    elif hasattr(value, "to_numpy"):  # pgvector.Vector when adapters are registered
        arr = np.asarray(value.to_numpy(), dtype=np.float32)
    else:
        arr = np.asarray(list(value), dtype=np.float32)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    return arr


def to_list(value) -> list[float] | None:
    arr = to_array(value)
    return None if arr is None else [float(x) for x in arr.tolist()]


def to_pgvector_text(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


# -------------------------------------------------------------------------------------------- detection
def configured_backend() -> str:
    value = str(getattr(settings, "VECTOR_BACKEND", AUTO) or AUTO).strip().lower()
    if value not in BACKENDS:
        raise ImproperlyConfigured(f"VECTOR_BACKEND={value!r}; use one of {', '.join(BACKENDS)}")
    return value


def _raw_id(connection) -> int | None:
    raw = getattr(connection, "connection", None)
    return id(raw) if raw is not None else None


def _query_bool(connection, sql: str, params=()) -> bool:
    with connection.cursor() as cur:
        cur.execute(sql, list(params))
        row = cur.fetchone()
    return bool(row and row[0])


def extension_installed(connection) -> bool:
    if getattr(connection, "vendor", "") != "postgresql":
        return False
    return _query_bool(connection, "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")


def extension_available(connection) -> bool:
    if getattr(connection, "vendor", "") != "postgresql":
        return False
    return _query_bool(connection, "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')")


def reset_cache(connection=None) -> None:
    """Forget cached detections (after the extension was created/dropped)."""
    targets = [connection] if connection is not None else [connections[a] for a in connections]
    for conn in targets:
        for attr in (_ATTR, _COLS_ATTR):
            if hasattr(conn, attr):
                delattr(conn, attr)


def effective_backend(connection=None) -> str:
    """"pgvector" or "array" for this connection (auto: is the `vector` extension installed?)."""
    cfg = configured_backend()
    if cfg != AUTO:
        return cfg
    connection = connection if connection is not None else connections["default"]
    if getattr(connection, "vendor", "") != "postgresql":
        return ARRAY
    cached = getattr(connection, _ATTR, None)
    raw = _raw_id(connection)
    if cached is not None and cached[0] == raw and raw is not None:
        return cached[1]
    try:
        backend = PGVECTOR if extension_installed(connection) else ARRAY
    except DatabaseError:
        log.warning("vectors: could not detect the vector extension; using the array backend", exc_info=True)
        return ARRAY
    setattr(connection, _ATTR, (_raw_id(connection), backend))
    return backend


def column_type(connection, table: str, column: str) -> str | None:
    """The column's SQL type as PostgreSQL formats it (e.g. 'vector(768)', 'double precision[]'), None if absent."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "WHERE a.attrelid = to_regclass(%s) AND a.attname = %s AND NOT a.attisdropped",
            [table, column],
        )
        row = cur.fetchone()
    return row[0] if row else None


def field_backend(field: models.Field, connection) -> str:
    """Backend of the column that exists in the database for `field` (falls back to effective_backend)."""
    if getattr(connection, "vendor", "") != "postgresql":
        return ARRAY
    model = getattr(field, "model", None)
    if model is None or not getattr(field, "column", None):
        return effective_backend(connection)
    key = (model._meta.db_table, field.column)
    raw = _raw_id(connection)
    cache = getattr(connection, _COLS_ATTR, None)
    if cache is not None and raw is not None and cache[0] == raw and key in cache[1]:
        return cache[1][key]
    try:
        kind = column_type(connection, *key)
    except DatabaseError:
        return effective_backend(connection)
    if kind is None:
        return effective_backend(connection)  # table not created yet (inside a migration)
    backend = PGVECTOR if kind.startswith("vector") else ARRAY
    raw = _raw_id(connection)
    entries = cache[1] if cache is not None and cache[0] == raw else {}
    entries[key] = backend
    setattr(connection, _COLS_ATTR, (raw, entries))
    return backend


# ------------------------------------------------------------------------------------------------ field
class EmbeddingField(models.Field):
    """Embedding column: `vector(dim)` with pgvector, `double precision[]` without it."""

    description = "Embedding vector (pgvector or double precision[])"
    empty_strings_allowed = False

    def __init__(self, dim: int | None = None, *args, **kwargs):
        if dim is None:
            raise TypeError("EmbeddingField requires dim")
        self.dim = int(dim)
        if self.dim <= 0:
            raise ValueError("EmbeddingField dim must be positive")
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["dim"] = self.dim
        return name, path, args, kwargs

    def db_type(self, connection) -> str:
        if effective_backend(connection) == PGVECTOR:
            return f"vector({self.dim})"
        return "double precision[]"

    def cast_db_type(self, connection) -> str:
        return self.db_type(connection)

    def rel_db_type(self, connection) -> str:  # pragma: no cover - never a FK target
        return self.db_type(connection)

    def _checked(self, value) -> list[float] | None:
        values = to_list(value)
        if values is None:
            return None
        if len(values) != self.dim:
            raise ValueError(f"expected {self.dim} dimensions, got {len(values)}")
        if not all(np.isfinite(values)):
            raise ValueError("embedding contains NaN or infinity")
        return values

    def get_prep_value(self, value):
        return self._checked(value)

    def get_db_prep_value(self, value, connection, prepared=False):
        values = value if prepared else self.get_prep_value(value)
        if values is None:
            return None
        if field_backend(self, connection) == PGVECTOR:
            return to_pgvector_text(values)
        return [float(x) for x in values]

    def from_db_value(self, value, expression, connection):
        return to_array(value)

    def to_python(self, value):
        return to_array(value)

    def value_to_string(self, obj) -> str:
        values = to_list(self.value_from_object(obj))
        return json.dumps(values)

    def formfield(self, **kwargs):  # pragma: no cover - never edited through forms
        return None


# ---------------------------------------------------------------------------------------------- nearest
class _VectorParam(Expression):
    """A query vector literal cast to `vector` (pgvector only)."""

    output_field = FloatField()

    def __init__(self, values: Sequence[float]):
        super().__init__()
        self.values = [float(x) for x in values]

    def as_sql(self, compiler, connection):
        return "%s::vector", [to_pgvector_text(self.values)]


class CosineDistance(Func):
    """pgvector cosine distance `a <=> b` (0 = same direction, 2 = opposite)."""

    arg_joiner = " <=> "
    template = "(%(expressions)s)"
    output_field = FloatField()

    def __init__(self, expression, values: Sequence[float]):
        super().__init__(expression if not isinstance(expression, str) else F(expression), _VectorParam(values))


def cosine_distances(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """1 - cosine similarity of every row of `matrix` with `query` (zero vectors get distance 1)."""
    matrix = np.asarray(matrix, dtype=np.float32)
    query = np.asarray(query, dtype=np.float32).reshape(-1)
    if matrix.size == 0:
        return np.zeros((0,), dtype=np.float32)
    qn = float(np.linalg.norm(query))
    norms = np.linalg.norm(matrix, axis=1)
    denom = norms * qn
    sims = np.zeros(matrix.shape[0], dtype=np.float64)
    ok = denom > 0
    sims[ok] = (matrix[ok] @ query) / denom[ok]
    return (1.0 - np.clip(sims, -1.0, 1.0)).astype(np.float64)


def rank_vectors(ids: Sequence, vectors: Sequence, query, k: int) -> list[tuple[object, float]]:
    """Pure numpy ranking: [(id, cosine distance)] of the k nearest, ties kept in input order."""
    q = to_array(query)
    if q is None or k <= 0 or not len(ids):
        return []
    keep_ids, rows = [], []
    for i, v in zip(ids, vectors, strict=True):
        arr = to_array(v)
        if arr is None or arr.shape[0] != q.shape[0]:
            continue
        keep_ids.append(i)
        rows.append(arr)
    if not rows:
        return []
    dist = cosine_distances(np.stack(rows), q)
    order = np.argsort(dist, kind="stable")[:k]
    return [(keep_ids[int(j)], float(dist[int(j)])) for j in order]


def nearest(queryset, field: str, query_vec, k: int, *, max_scan: int = DEFAULT_MAX_SCAN) -> list:
    """The k model instances nearest to `query_vec` by cosine distance, each with `.distance` set.

    The caller filters `queryset` (org via RLS, dataset/document scope) BEFORE calling this.
    """
    q = to_array(query_vec)
    if q is None or k <= 0 or not np.any(q):
        return []
    model_field = queryset.model._meta.get_field(field)
    connection = connections[queryset.db]
    qs = queryset.filter(**{f"{field}__isnull": False})
    if field_backend(model_field, connection) == PGVECTOR:
        return list(qs.annotate(distance=CosineDistance(field, q.tolist())).order_by("distance")[:k])
    pk_name = queryset.model._meta.pk.attname
    candidates = list(qs.order_by("-pk").values_list(pk_name, field)[:max(1, int(max_scan))])
    if not candidates:
        return []
    ranked = rank_vectors([c[0] for c in candidates], [c[1] for c in candidates], q, k)
    objs = qs.in_bulk([pk for pk, _ in ranked])
    out = []
    for pk, dist in ranked:
        obj = objs.get(pk)
        if obj is not None:
            obj.distance = dist
            out.append(obj)
    return out


# --------------------------------------------------------------------------------------- migration ops
def _ensure_extension(apps, schema_editor) -> None:
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    cfg = configured_backend()
    if cfg == ARRAY:
        reset_cache(connection)
        return
    if extension_installed(connection):
        reset_cache(connection)
        return
    if not extension_available(connection):
        if cfg == PGVECTOR:
            raise ImproperlyConfigured(
                "VECTOR_BACKEND=pgvector but the `vector` extension is not available in this PostgreSQL. "
                "Install pgvector or set VECTOR_BACKEND=array (or auto).")
        log.info("vectors: pgvector not available; embeddings use double precision[] (array backend)")
        reset_cache(connection)
        return
    try:
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except DatabaseError as exc:
        if cfg == PGVECTOR:
            raise ImproperlyConfigured(
                f"VECTOR_BACKEND=pgvector but CREATE EXTENSION vector failed ({exc}). Create it as a superuser "
                "(scripts/bootstrap_local_db.py does) or set VECTOR_BACKEND=auto.") from exc
        log.warning("vectors: CREATE EXTENSION vector failed (%s); using the array backend", type(exc).__name__)
    reset_cache(connection)


def _noop(apps, schema_editor) -> None:
    """Reverse of the extension step: the extension is shared database state and is left in place."""
    reset_cache(schema_editor.connection)


def ensure_vector_extension() -> migrations.RunPython:
    """RunPython: create the `vector` extension when VECTOR_BACKEND allows it and pg_available_extensions has it."""
    return migrations.RunPython(_ensure_extension, _noop, elidable=False)


def _index_sql(table: str, column: str, name: str, m: int, ef_construction: int) -> str:
    return (f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" USING hnsw ("{column}" vector_cosine_ops) '
            f"WITH (m = {int(m)}, ef_construction = {int(ef_construction)})")


def hnsw_index(table: str, column: str, name: str, *, m: int = 16, ef_construction: int = 64) -> migrations.RunPython:
    """RunPython: an HNSW cosine index on `table.column`, created only when that column is a pgvector column."""

    def forward(apps, schema_editor):
        connection = schema_editor.connection
        if connection.vendor != "postgresql":
            return
        kind = column_type(connection, table, column) or ""
        if kind.startswith("vector"):
            schema_editor.execute(_index_sql(table, column, name, m, ef_construction), params=None)

    def backward(apps, schema_editor):
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(f'DROP INDEX IF EXISTS "{name}"', params=None)

    return migrations.RunPython(forward, backward, elidable=False)
