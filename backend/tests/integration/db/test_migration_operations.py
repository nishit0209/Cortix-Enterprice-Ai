"""The custom migration operations must be executable through Django's PostgreSQL schema editor.

DatabaseSchemaEditor.execute(sql, params=()) merges params client-side (connection.ops.compose_sql), so any
literal '%' in SQL passed with params=() must be escaped or the SQL must be passed with params=None.
"""

from __future__ import annotations

import pytest
from django.db import connection

from apps.core.tenancy.operations import EnableTenantRLS, MakeAppendOnly

pytestmark = pytest.mark.django_db


class _RecordingEditor:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=()):
        self.calls.append((str(sql), params))


@pytest.mark.parametrize(
    "operation",
    [
        MakeAppendOnly("qa_probe_table"),
        EnableTenantRLS("qa_probe_table"),
        EnableTenantRLS("qa_probe", column="id"),
    ],
    ids=["MakeAppendOnly", "EnableTenantRLS", "EnableTenantRLS-id"],
)
@pytest.mark.parametrize("direction", ["database_forwards", "database_backwards"])
def test_operation_sql_survives_schema_editor_param_merging(operation, direction):
    """DEFECT core-append-only-percent (when failing): MakeAppendOnly's plpgsql `RAISE ... 'table % ...'` is
    passed with params=() so psycopg raises 'incomplete placeholder' and `migrate` fails on a fresh database."""
    editor = _RecordingEditor()
    getattr(operation, direction)("core", editor, None, None)
    assert editor.calls
    for sql, params in editor.calls:
        if params is None:
            continue  # executed verbatim
        connection.ops.compose_sql(sql, params)  # exactly what DatabaseSchemaEditor.execute() does
