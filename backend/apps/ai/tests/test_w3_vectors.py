"""apps.ai.vectors without a database: backend detection, the field's column type and conversions, numpy ranking
and the migration operations (fake connections; the DB-backed behaviour is in test_w3_db.py)."""

from __future__ import annotations

import numpy as np
import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.ai import vectors
from apps.ai.vectors import (
    ARRAY,
    PGVECTOR,
    EmbeddingField,
    cosine_distances,
    effective_backend,
    rank_vectors,
    to_array,
    to_list,
    to_pgvector_text,
)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "FROM pg_extension" in sql:
            self._row = (self.conn.installed,)
        elif "FROM pg_available_extensions" in sql:
            self._row = (self.conn.available,)
        elif "format_type" in sql:
            self._row = (self.conn.column_types.get(tuple(params)),) if tuple(params) in self.conn.column_types \
                else None
        elif sql.startswith("CREATE EXTENSION"):
            if self.conn.create_fails:
                from django.db import DatabaseError

                raise DatabaseError("permission denied to create extension")
            self.conn.installed = True
        else:
            self._row = None

    def fetchone(self):
        return self._row


class FakeConnection:
    vendor = "postgresql"
    alias = "default"

    def __init__(self, *, installed=False, available=False, create_fails=False, column_types=None):
        self.installed = installed
        self.available = available
        self.create_fails = create_fails
        self.column_types = column_types or {}
        self.executed: list = []
        self.connection = object()  # the "raw" connection identity used for the per-connection cache

    def cursor(self):
        return FakeCursor(self)

    def queries(self, needle: str) -> int:
        return sum(1 for sql, _ in self.executed if needle in sql)


class FakeSchemaEditor:
    def __init__(self, connection):
        self.connection = connection
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(sql)


@pytest.fixture(autouse=True)
def _no_atomic(monkeypatch):
    """transaction.atomic(using=<fake alias>) would open a real connection; the fake needs no savepoint."""
    from contextlib import nullcontext

    monkeypatch.setattr(vectors.transaction, "atomic", lambda *a, **k: nullcontext())


# ------------------------------------------------------------------------------------------- detection
@pytest.mark.parametrize(("setting", "installed", "expected"), [
    ("pgvector", False, PGVECTOR), ("array", True, ARRAY), ("auto", True, PGVECTOR), ("auto", False, ARRAY),
    ("AUTO", True, PGVECTOR),
])
def test_effective_backend(settings, setting, installed, expected):
    settings.VECTOR_BACKEND = setting
    assert effective_backend(FakeConnection(installed=installed)) == expected


def test_auto_detection_is_cached_per_connection(settings):
    settings.VECTOR_BACKEND = "auto"
    conn = FakeConnection(installed=True)
    assert effective_backend(conn) == PGVECTOR
    assert effective_backend(conn) == PGVECTOR
    assert conn.queries("pg_extension") == 1
    conn.connection = object()  # reconnect -> detect again
    conn.installed = False
    assert effective_backend(conn) == ARRAY
    assert conn.queries("pg_extension") == 2
    vectors.reset_cache(conn)
    conn.installed = True
    assert effective_backend(conn) == PGVECTOR


def test_non_postgres_and_bad_setting(settings):
    settings.VECTOR_BACKEND = "auto"

    class Sqlite:
        vendor = "sqlite"

    assert effective_backend(Sqlite()) == ARRAY
    settings.VECTOR_BACKEND = "faiss"
    with pytest.raises(ImproperlyConfigured):
        effective_backend(FakeConnection())


# ------------------------------------------------------------------------------------------------ field
def test_field_db_type_follows_the_backend(settings):
    field = EmbeddingField(dim=768, null=True)
    settings.VECTOR_BACKEND = "auto"
    assert field.db_type(FakeConnection(installed=True)) == "vector(768)"
    assert field.db_type(FakeConnection(installed=False)) == "double precision[]"
    settings.VECTOR_BACKEND = "pgvector"
    assert field.db_type(FakeConnection()) == "vector(768)"
    assert field.cast_db_type(FakeConnection()) == "vector(768)"
    settings.VECTOR_BACKEND = "array"
    assert field.db_type(FakeConnection(installed=True)) == "double precision[]"


def test_field_deconstruct_and_validation():
    field = EmbeddingField(dim=4, null=True, blank=True)
    _name, path, args, kwargs = field.deconstruct()
    assert path == "apps.ai.vectors.EmbeddingField" and kwargs["dim"] == 4 and kwargs["null"] is True
    with pytest.raises(TypeError):
        EmbeddingField()
    with pytest.raises(ValueError):
        EmbeddingField(dim=0)
    with pytest.raises(ValueError, match="expected 4 dimensions"):
        field.get_prep_value([1, 2, 3])
    with pytest.raises(ValueError, match="NaN"):
        field.get_prep_value([1, 2, float("nan"), 4])


def test_field_db_values_per_backend(monkeypatch):
    field = EmbeddingField(dim=3)
    conn = FakeConnection()
    monkeypatch.setattr(vectors, "field_backend", lambda f, c: PGVECTOR)
    assert field.get_db_prep_value(np.array([1.0, 0.5, -2.0], dtype=np.float32), conn) == "[1.0,0.5,-2.0]"
    monkeypatch.setattr(vectors, "field_backend", lambda f, c: ARRAY)
    value = field.get_db_prep_value((1, 0.5, -2), conn)
    assert value == [1.0, 0.5, -2.0] and all(isinstance(v, float) for v in value)
    assert field.get_db_prep_value(None, conn) is None


@pytest.mark.parametrize("raw", ["[1,0.5,-2]", "{1,0.5,-2}", [1, 0.5, -2], (1, 0.5, -2), np.array([1, 0.5, -2])])
def test_from_db_value_accepts_both_backends(raw):
    out = EmbeddingField(dim=3).from_db_value(raw, None, None)
    assert isinstance(out, np.ndarray) and out.dtype == np.float32
    assert out.tolist() == [1.0, 0.5, -2.0]


def test_conversion_helpers():
    assert to_array(None) is None and to_array("") is None
    assert to_list(np.array([[1, 2]])) == [1.0, 2.0]  # flattened
    assert to_pgvector_text([0.25, 1]) == "[0.25,1.0]"

    class Vec:  # pgvector.Vector-like
        def to_numpy(self):
            return np.array([3.0, 4.0])

    assert to_list(Vec()) == [3.0, 4.0]


def test_field_backend_reads_the_real_column_type(settings):
    settings.VECTOR_BACKEND = "auto"

    class Model:
        class _meta:
            db_table = "documents_documentchunk"

    field = EmbeddingField(dim=3)
    field.model, field.column = Model, "embedding"
    conn = FakeConnection(installed=True,
                          column_types={("documents_documentchunk", "embedding"): "double precision[]"})
    # pgvector is installed, but this column was migrated without it -> the column wins
    assert vectors.field_backend(field, conn) == ARRAY
    assert vectors.field_backend(field, conn) == ARRAY
    assert conn.queries("format_type") == 1  # cached per connection
    missing = FakeConnection(installed=True)  # table not created yet -> setting/detection
    assert vectors.field_backend(field, missing) == PGVECTOR


# -------------------------------------------------------------------------------------------- ranking
def test_cosine_distances_and_rank():
    m = np.array([[1, 0], [0, 1], [1, 1], [-1, 0], [0, 0]], dtype=np.float32)
    d = cosine_distances(m, np.array([1, 0]))
    assert d.round(6).tolist() == [0.0, 1.0, round(1 - 1 / np.sqrt(2), 6), 2.0, 1.0]
    ranked = rank_vectors(["a", "b", "c", "d", "zero"], m, [1, 0], 3)
    assert [i for i, _ in ranked] == ["a", "c", "b"]
    assert ranked[0][1] == pytest.approx(0.0)


def test_rank_skips_wrong_dimensions_and_keeps_ties_in_order():
    ranked = rank_vectors([1, 2, 3, 4], [[1, 0], [1, 0, 0], [2, 0], None], [3, 0], 10)
    assert ranked == [(1, pytest.approx(0.0)), (3, pytest.approx(0.0))]
    assert rank_vectors([], [], [1], 3) == [] and rank_vectors([1], [[1]], [1], 0) == []


def test_nearest_rejects_empty_queries():
    assert vectors.nearest(None, "embedding", None, 5) == []
    assert vectors.nearest(None, "embedding", [0.0, 0.0], 5) == []


# ---------------------------------------------------------------------------------------- migration ops
def test_extension_step_creates_it_only_when_allowed(settings):
    op = vectors.ensure_vector_extension()
    settings.VECTOR_BACKEND = "auto"
    conn = FakeConnection(available=True)
    op.code(None, FakeSchemaEditor(conn))
    assert conn.queries("CREATE EXTENSION IF NOT EXISTS vector") == 1 and conn.installed
    assert effective_backend(conn) == PGVECTOR

    unavailable = FakeConnection(available=False)
    op.code(None, FakeSchemaEditor(unavailable))
    assert unavailable.queries("CREATE EXTENSION") == 0 and effective_backend(unavailable) == ARRAY

    settings.VECTOR_BACKEND = "array"
    arr = FakeConnection(available=True)
    op.code(None, FakeSchemaEditor(arr))
    assert arr.queries("pg_available_extensions") == 0 and arr.queries("CREATE EXTENSION") == 0


def test_extension_step_auto_tolerates_permission_errors(settings):
    settings.VECTOR_BACKEND = "auto"
    conn = FakeConnection(available=True, create_fails=True)
    vectors.ensure_vector_extension().code(None, FakeSchemaEditor(conn))
    assert not conn.installed and effective_backend(conn) == ARRAY


def test_extension_step_forced_pgvector_fails_clearly(settings):
    settings.VECTOR_BACKEND = "pgvector"
    with pytest.raises(ImproperlyConfigured, match="not available"):
        vectors.ensure_vector_extension().code(None, FakeSchemaEditor(FakeConnection(available=False)))
    with pytest.raises(ImproperlyConfigured, match="CREATE EXTENSION vector failed"):
        vectors.ensure_vector_extension().code(
            None, FakeSchemaEditor(FakeConnection(available=True, create_fails=True)))


def test_extension_step_is_reversible():
    op = vectors.ensure_vector_extension()
    assert op.reversible
    editor = FakeSchemaEditor(FakeConnection(installed=True))
    op.reverse_code(None, editor)
    assert editor.sql == []  # the shared extension is never dropped


def test_hnsw_index_only_on_vector_columns():
    op = vectors.hnsw_index("documents_documentchunk", "embedding", "doc_chunk_emb_hnsw")
    vec = FakeSchemaEditor(FakeConnection(column_types={("documents_documentchunk", "embedding"): "vector(768)"}))
    op.code(None, vec)
    assert vec.sql == ['CREATE INDEX IF NOT EXISTS "doc_chunk_emb_hnsw" ON "documents_documentchunk" USING hnsw '
                       '("embedding" vector_cosine_ops) WITH (m = 16, ef_construction = 64)']
    arr = FakeSchemaEditor(FakeConnection(
        column_types={("documents_documentchunk", "embedding"): "double precision[]"}))
    op.code(None, arr)
    assert arr.sql == []
    op.reverse_code(None, arr)
    assert arr.sql == ['DROP INDEX IF EXISTS "doc_chunk_emb_hnsw"']


def test_migrations_use_the_vector_operations():
    """Both migrations start with the extension step and end with the conditional HNSW index."""
    import importlib

    for module, index in (("apps.nl2sql.migrations.0001_initial", "nl2sql_cache_emb_hnsw"),
                          ("apps.documents.migrations.0001_initial", "doc_chunk_emb_hnsw")):
        ops = importlib.import_module(module).Migration.operations
        assert ops[0].code is vectors._ensure_extension
        assert ops[-1].code.__qualname__.startswith("hnsw_index")
        assert ops[-1].reversible
        assert index in __import__("inspect").getsource(importlib.import_module(module))
        fields = [f for op in ops if hasattr(op, "fields") for name, f in op.fields if name == "embedding"]
        assert fields and all(isinstance(f, EmbeddingField) and f.dim == 768 for f in fields)
