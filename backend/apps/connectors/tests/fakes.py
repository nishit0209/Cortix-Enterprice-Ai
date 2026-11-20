"""A scripted in-memory connector used by the DB-backed API/import tests (no network)."""

from __future__ import annotations

from typing import Any

from apps.connectors.base import (
    BaseConnector,
    Batch,
    ColumnInfo,
    CursorState,
    ReadonlyCheck,
    TableInfo,
    TestResult,
)
from apps.connectors.exceptions import ColumnNotFound, TableNotFound
from apps.connectors.postgres import PostgresConfig


class FakeConnector(BaseConnector):
    type = "postgresql"
    config_schema = PostgresConfig

    def __init__(self, *, readonly: bool = True, tables: dict[str, tuple[list[ColumnInfo], list[tuple]]] | None = None,
                 password_seen: list | None = None):
        self.readonly = readonly
        self.tables = tables or {}
        self.reads: list[dict[str, Any]] = []
        self.password_seen = password_seen

    def test(self) -> TestResult:
        if not self.readonly:
            return TestResult(ok=False, server_version="17.2", current_user="writer", database="shop",
                              resolved_ip="52.10.20.30", readonly=ReadonlyCheck(ok=False, problems=("write:sales.orders",)),
                              code="connector_role_not_readonly",
                              message="The database role has write or administrative privileges; use a read-only role.")
        return TestResult(ok=True, server_version="17.2", current_user="nexora_ro", database="shop",
                          resolved_ip="52.10.20.30", readonly=ReadonlyCheck(ok=True), latency_ms=3, message="ok")

    def introspect(self) -> list[TableInfo]:
        return [TableInfo(schema="sales", name=name, kind="table", estimated_rows=len(rows), columns=tuple(cols))
                for name, (cols, rows) in self.tables.items()]

    def read_batches(self, table, columns=None, batch_rows=10_000, *, schema=None, cursor_column=None, after=None,
                     max_rows=None):
        if table not in self.tables:
            raise TableNotFound()
        cols, rows = self.tables[table]
        names = [c.name for c in cols]
        wanted = list(columns) if columns else names
        if any(c not in names for c in wanted):
            raise ColumnNotFound()
        idx = [names.index(c) for c in wanted]
        out = [tuple(r[i] for i in idx) for r in rows]
        if cursor_column:
            ci = names.index(cursor_column)
            pairs = sorted(zip(rows, out, strict=True), key=lambda p: p[0][ci])
            out = [o for r, o in pairs if after is None or str(r[ci]) > str(after)]
        self.reads.append({"table": table, "columns": wanted, "after": after})
        meta = tuple(cols[i] for i in idx)
        if not out:
            yield Batch(columns=meta, rows=[])
            return
        for start in range(0, len(out), batch_rows):
            yield Batch(columns=meta, rows=out[start:start + batch_rows])

    def incremental_cursor(self, table, cursor_column, *, schema=None):
        cols, rows = self.tables[table]
        ci = [c.name for c in cols].index(cursor_column)
        return CursorState(column=cursor_column, value=max(r[ci] for r in rows) if rows else None)


ORDERS_COLUMNS = [ColumnInfo("id", "bigint", False, 1), ColumnInfo("city", "text", True, 2),
                  ColumnInfo("total", "numeric(12,2)", True, 3)]
ORDERS_ROWS = [(i, ["Pune", "Mumbai", "Delhi"][i % 3], f"{i * 10}.50") for i in range(1, 31)]
