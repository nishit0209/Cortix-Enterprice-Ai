"""Regression tests (no database) for the wave-1 core defects reported by QA."""

from __future__ import annotations

from apps.core.tenancy.operations import EnableTenantRLS, MakeAppendOnly


class _RecordingEditor:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=()):
        self.calls.append((str(sql), params))


def test_migration_operations_execute_sql_verbatim():
    """SQL containing a literal '%' must reach the schema editor with params=None (no client-side merging)."""
    for op in (MakeAppendOnly("t_probe"), EnableTenantRLS("t_probe"), EnableTenantRLS("t_probe", column="id")):
        for direction in ("database_forwards", "database_backwards"):
            editor = _RecordingEditor()
            getattr(op, direction)("core", editor, None, None)
            assert editor.calls
            assert all(params is None for _, params in editor.calls), (op, direction)
    editor = _RecordingEditor()
    MakeAppendOnly("t_probe").database_forwards("core", editor, None, None)
    assert "%" in editor.calls[0][0]  # the RAISE placeholder is still there, executed verbatim
