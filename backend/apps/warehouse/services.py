"""Public service API of apps.warehouse (per-org schemas in `nexora_wh`).

    provision(org_id)                         -> WhOrgBinding   (idempotent; also run on org_created)
    ensure_provisioned(org_id)                -> WhOrgBinding
    reader_role(org_id)                       -> "wh_q_<short_id>"
    physical_table(dataset_version)           -> "org_<short_id>.ds_<short>_v<n>"
    load_table(org_id, table, columns, df)    -> LoadResult     (immutable table, COPY, ANALYZE)
    read_table(org_id, table, columns)        -> polars.DataFrame (internal system reads, e.g. quality)
    drop_table(org_id, table)                 -> None           (retention / failed-load cleanup)
    execute_readonly_sql(org_id, sql, ...)    -> nexora.contracts.ResultSet  (low-level guarded executor)

Every function refuses to act for an org other than the active tenant context.
"""

from __future__ import annotations

import io
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import polars as pl
import psycopg
from django.conf import settings
from psycopg import errors as pg_errors
from psycopg import sql

from apps.core.tenancy import get_current_org_id, require_org_id, tenant_context
from nexora.contracts import ColumnMeta, ResultSet

from . import naming
from .db import executor_connection, loader_connection
from .exceptions import (
    WarehouseError,
    WarehouseQueryDenied,
    WarehouseQueryFailed,
    WarehouseQueryTimeout,
    WarehouseTenantMismatch,
    WarehouseUnavailable,
)
from .models import WhOrgBinding

log = logging.getLogger(__name__)

PG_TYPES: dict[str, pl.DataType] = {
    "boolean": pl.Boolean(),
    "bigint": pl.Int64(),
    "double precision": pl.Float64(),
    "numeric": pl.Float64(),
    "date": pl.Date(),
    "timestamptz": pl.Datetime("us", "UTC"),
    "text": pl.String(),
    "jsonb": pl.String(),
}
COPY_BATCH_ROWS = 50_000
_TYPE_ALIASES = {
    "int8": "bigint", "int4": "integer", "int2": "smallint", "float8": "double precision", "float4": "real",
    "bool": "boolean", "varchar": "text", "bpchar": "text", "timestamp": "timestamp", "timestamptz": "timestamptz",
}
# Defense in depth for the low-level executor (the SQL guard is the primary control): statements that could
# change the session role/settings are refused before they reach the database.
_EXECUTOR_DENY_RE = re.compile(
    r"\b(set_config|set\s+(local\s+|session\s+)?(role|session\s+authorization)|reset\s+(role|all|session)|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    pg_type: str


@dataclass(frozen=True)
class LoadResult:
    wh_schema: str
    wh_table: str
    row_count: int
    byte_size: int

    @property
    def physical(self) -> str:
        return f"{self.wh_schema}.{self.wh_table}"


# --------------------------------------------------------------------------------------------------
# tenant guards
# --------------------------------------------------------------------------------------------------
def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _assert_active_org(org_id: UUID | str) -> UUID:
    """The org must be the active tenant (fail closed when no context is active)."""
    org_uuid = _uuid(org_id)
    active = require_org_id()
    if active != org_uuid:
        raise WarehouseTenantMismatch()
    return org_uuid


def _org_short_id(org_uuid: UUID) -> str:
    from apps.orgs.services import get_current_org

    with tenant_context(org_uuid):
        return get_current_org().short_id


# --------------------------------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------------------------------
def _provision_objects(schema: str, role: str, executor: str) -> None:
    ident = sql.Identifier
    with loader_connection() as conn:
        with conn.transaction():
            cur = conn.cursor()
            # serialize concurrent provisioning of the same org
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"nexora_wh_provision:{schema}"])
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(ident(schema)))
            cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(ident(schema)))
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(ident(role)))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(ident(schema), ident(role)))
            cur.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}").format(
                    ident(schema), ident(role)
                )
            )
            cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(ident(schema), ident(role)))
            cur.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE r.rolname = %s AND u.rolname = %s",
                [role, executor],
            )
            if cur.fetchone() is None:
                cur.execute(sql.SQL("GRANT {} TO {}").format(ident(role), ident(executor)))


def provision(org_id: UUID | str) -> WhOrgBinding:
    """Create (idempotently) the org schema + NOLOGIN reader role and record the binding."""
    org_uuid = _uuid(org_id)
    active = get_current_org_id()
    if active is not None and active != org_uuid:
        raise WarehouseTenantMismatch()
    short_id = _org_short_id(org_uuid)
    schema = naming.schema_name(short_id)
    role = naming.reader_role_name(short_id)
    _provision_objects(schema, role, naming.executor_login_role())
    with tenant_context(org_uuid):
        binding = WhOrgBinding.objects.filter(org_id=org_uuid).first()
        if binding is None:
            binding = WhOrgBinding.objects.create(org_id=org_uuid, wh_schema=schema, reader_role=role, status="ready")
        elif (binding.wh_schema, binding.reader_role, binding.status) != (schema, role, "ready"):
            binding.wh_schema, binding.reader_role, binding.status = schema, role, "ready"
            binding.save(update_fields=["wh_schema", "reader_role", "status", "updated_at"])
    log.info("warehouse provisioned org=%s schema=%s role=%s", org_uuid, schema, role)
    return binding


def get_binding(org_id: UUID | str) -> WhOrgBinding | None:
    org_uuid = _assert_active_org(org_id)
    return WhOrgBinding.objects.filter(org_id=org_uuid).first()


def ensure_provisioned(org_id: UUID | str) -> WhOrgBinding:
    binding = get_binding(org_id)
    if binding is not None and binding.status == "ready":
        return binding
    return provision(org_id)


def reader_role(org_id: UUID | str) -> str:
    return ensure_provisioned(org_id).reader_role


def org_schema(org_id: UUID | str) -> str:
    return ensure_provisioned(org_id).wh_schema


def physical_table(dataset_version: Any) -> str:
    """'schema.table' of a dataset version (any object with wh_schema/wh_table and org_id)."""
    _assert_active_org(dataset_version.org_id)
    schema = naming.check_ident(dataset_version.wh_schema)
    table = naming.check_ident(dataset_version.wh_table)
    return f"{schema}.{table}"


def drop_org_objects(short_id: str) -> None:
    """Drop an org's schema (CASCADE) and reader role. Used by the org purge job and test cleanup."""
    schema = naming.schema_name(short_id)
    role = naming.reader_role_name(short_id)
    with loader_connection(autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


# --------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------
def _column_defs(columns: Sequence[ColumnSpec]) -> sql.Composed:
    parts = []
    for col in columns:
        if col.pg_type not in PG_TYPES:
            raise WarehouseError(f"unsupported column type {col.pg_type!r}", code="warehouse_bad_type")
        parts.append(sql.SQL("{} {}").format(sql.Identifier(naming.check_ident(col.name)), sql.SQL(col.pg_type)))
    return sql.SQL(", ").join(parts)


def prepare_frame(df: pl.DataFrame, columns: Sequence[ColumnSpec]) -> pl.DataFrame:
    """Select + cast the frame to the exact physical types (strict: a mismatch is a bug, not data)."""
    exprs = []
    for col in columns:
        target = PG_TYPES[col.pg_type]
        e = pl.col(col.name)
        src = df.schema[col.name]
        if col.pg_type == "timestamptz":
            if isinstance(src, pl.Datetime) and src.time_zone is None:
                e = e.dt.replace_time_zone("UTC")
            elif isinstance(src, pl.Datetime):
                e = e.dt.convert_time_zone("UTC")
            else:
                e = e.cast(target, strict=True)
        elif col.pg_type in ("text", "jsonb"):
            e = e.cast(pl.String, strict=True).str.replace_all("\x00", "", literal=True)
        else:
            e = e.cast(target, strict=True)
        exprs.append(e.alias(col.name))
    return df.select(exprs)


def _frame_to_csv(frame: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    frame.write_csv(
        buf,
        include_header=False,
        quote_style="non_numeric",  # strings always quoted: "" is an empty string, unquoted empty is NULL
        datetime_format="%Y-%m-%dT%H:%M:%S%.f%:z",
        date_format="%Y-%m-%d",
        null_value="",
    )
    return buf.getvalue()


def load_table(
    org_id: UUID | str, table: str, columns: Sequence[ColumnSpec], df: pl.DataFrame, *, batch_rows: int = COPY_BATCH_ROWS
) -> LoadResult:
    """Create the immutable table `<org schema>.<table>` and COPY the frame into it, then ANALYZE."""
    org_uuid = _assert_active_org(org_id)
    binding = ensure_provisioned(org_uuid)
    schema = naming.check_ident(binding.wh_schema)
    table = naming.check_ident(table)
    if not columns:
        raise WarehouseError("a table needs at least one column", code="warehouse_no_columns")
    frame = prepare_frame(df, columns)
    qualified = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    col_list = sql.SQL(", ").join(sql.Identifier(c.name) for c in columns)
    rows = 0
    try:
        with loader_connection() as conn:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute("SELECT set_config('TimeZone', 'UTC', true)")
                cur.execute(sql.SQL("CREATE TABLE {} ({})").format(qualified, _column_defs(columns)))
                copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN (FORMAT csv)").format(qualified, col_list)
                with cur.copy(copy_stmt) as copy:
                    for chunk in frame.iter_slices(n_rows=max(1, batch_rows)):
                        copy.write(_frame_to_csv(chunk))
                rows = cur.rowcount if cur.rowcount and cur.rowcount > 0 else frame.height
                cur.execute(
                    sql.SQL("GRANT SELECT ON {} TO {}").format(qualified, sql.Identifier(binding.reader_role))
                )
                cur.execute(
                    sql.SQL("COMMENT ON TABLE {} IS {}").format(qualified, sql.Literal("nexora:immutable dataset version"))
                )
            conn.execute(sql.SQL("ANALYZE {}").format(qualified))
            conn.commit()
            size = conn.execute("SELECT pg_total_relation_size(%s::regclass)", [f'"{schema}"."{table}"']).fetchone()[0]
            conn.commit()
    except pg_errors.DuplicateTable as exc:
        raise WarehouseError("the dataset version table already exists", code="warehouse_table_exists") from exc
    except psycopg.OperationalError as exc:
        raise WarehouseUnavailable() from exc
    except psycopg.Error as exc:
        raise WarehouseError(f"warehouse load failed: {exc.diag.message_primary or type(exc).__name__}",
                             code="warehouse_load_failed") from exc
    return LoadResult(wh_schema=schema, wh_table=table, row_count=int(rows), byte_size=int(size or 0))


def drop_table(org_id: UUID | str, table: str) -> None:
    org_uuid = _assert_active_org(org_id)
    binding = get_binding(org_uuid)
    if binding is None:
        return
    with loader_connection(autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                sql.Identifier(naming.check_ident(binding.wh_schema)), sql.Identifier(naming.check_ident(table))
            )
        )


def _cast_from_pg_text(series: pl.Series, pg_type: str) -> pl.Series:
    if pg_type == "bigint":
        return series.cast(pl.Int64, strict=False)
    if pg_type in ("double precision", "numeric"):
        return series.cast(pl.Float64, strict=False)
    if pg_type == "boolean":
        return series.replace_strict({"t": True, "f": False}, default=None, return_dtype=pl.Boolean)
    if pg_type == "date":
        return series.str.strptime(pl.Date, "%Y-%m-%d", strict=False)
    if pg_type == "timestamptz":
        return series.str.strptime(pl.Datetime("us", "UTC"), "%Y-%m-%d %H:%M:%S%.f%#z", strict=False)
    return series


def read_table(
    org_id: UUID | str, table: str, columns: Sequence[ColumnSpec], *, limit: int | None = None
) -> pl.DataFrame:
    """Read a dataset version table back into Polars (system reads as the loader; never user SQL)."""
    org_uuid = _assert_active_org(org_id)
    binding = ensure_provisioned(org_uuid)
    schema = naming.check_ident(binding.wh_schema)
    table = naming.check_ident(table)
    names = [naming.check_ident(c.name) for c in columns]
    select = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(n) for n in names), sql.Identifier(schema), sql.Identifier(table)
    )
    if limit is not None:
        select = select + sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
    buf = io.BytesIO()
    try:
        with loader_connection() as conn:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute("SELECT set_config('TimeZone', 'UTC', true)")
                with cur.copy(sql.SQL("COPY ({}) TO STDOUT (FORMAT csv)").format(select)) as copy:
                    for block in copy:
                        buf.write(block)
    except psycopg.OperationalError as exc:
        raise WarehouseUnavailable() from exc
    except psycopg.Error as exc:
        raise WarehouseError(f"warehouse read failed: {exc.diag.message_primary or type(exc).__name__}",
                             code="warehouse_read_failed") from exc
    data = buf.getvalue()
    if not data:
        return pl.DataFrame({n: pl.Series(n, [], dtype=PG_TYPES[c.pg_type]) for n, c in zip(names, columns, strict=True)})
    # PG CSV: NULL is an unquoted empty field, '' is a quoted empty field; Polars keeps that distinction.
    raw = pl.read_csv(
        io.BytesIO(data), has_header=False, new_columns=names, schema={n: pl.String for n in names}
    )
    return pl.DataFrame([_cast_from_pg_text(raw[n], c.pg_type).alias(n) for n, c in zip(names, columns, strict=True)])


# --------------------------------------------------------------------------------------------------
# low-level read-only executor (used by the SQL guard; never call with unvalidated user SQL directly)
# --------------------------------------------------------------------------------------------------
def _row_bytes(row: Sequence[Any]) -> int:
    total = 8
    for v in row:
        if v is None:
            total += 1
        elif isinstance(v, (bytes, bytearray, memoryview)):
            total += len(v)
        elif isinstance(v, str):
            total += len(v.encode("utf-8", "ignore"))
        else:
            total += len(str(v))
    return total


def _type_name(conn: psycopg.Connection, oid: int) -> str:
    info = conn.adapters.types.get(oid)
    name = info.name if info is not None else "unknown"
    return _TYPE_ALIASES.get(name, name)


def execute_readonly_sql(
    org_id: UUID | str,
    sql_text: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    timeout_s: int | float | None = None,
    row_cap: int | None = None,
    byte_cap: int | None = None,
) -> ResultSet:
    """Run ONE read-only statement as the org's reader role and return capped rows.

    BEGIN READ ONLY -> SET LOCAL ROLE <reader> -> SET LOCAL statement_timeout/search_path -> DECLARE cursor
    -> fetch until row_cap / byte_cap -> ROLLBACK. Refuses to run for any org but the active tenant.
    """
    org_uuid = _assert_active_org(org_id)
    binding = ensure_provisioned(org_uuid)
    role = naming.check_ident(binding.reader_role)
    schema = naming.check_ident(binding.wh_schema)
    timeout_ms = max(100, int(float(timeout_s or settings.NL2SQL_TIMEOUT_S) * 1000))
    row_cap = max(0, int(row_cap if row_cap is not None else settings.NL2SQL_ROW_CAP))
    byte_cap = max(0, int(byte_cap if byte_cap is not None else settings.NL2SQL_BYTE_CAP))
    if not sql_text or not sql_text.strip():
        raise WarehouseQueryFailed("empty statement", code="query_empty")
    if _EXECUTOR_DENY_RE.search(sql_text):
        raise WarehouseQueryDenied("statement uses a forbidden command or function", code="query_forbidden")

    started = time.monotonic()
    conn = executor_connection()
    try:
        conn.read_only = True  # psycopg issues BEGIN READ ONLY on first statement
        cur = conn.cursor()
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        cur.execute(
            "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true), "
            "set_config('idle_in_transaction_session_timeout', %s, true), set_config('search_path', %s, true), "
            "set_config('TimeZone', 'UTC', true)",
            [str(timeout_ms), "2000", str(timeout_ms + 10_000), f'"{schema}"'],
        )
        cur.execute("SELECT current_user")
        if cur.fetchone()[0] != role:  # pragma: no cover - defensive
            raise WarehouseQueryDenied("reader role could not be assumed", code="query_role_error")
        rows: list[tuple] = []
        truncated = False
        nbytes = 0
        deadline = time.monotonic() + timeout_ms / 1000
        with conn.cursor(name=f"nexora_q_{uuid4().hex[:16]}") as qc:
            qc.itersize = max(1, min(row_cap + 1, 2000))
            qc.execute(sql_text, params or None)
            description = qc.description or []
            columns = tuple(ColumnMeta(name=d.name, type=_type_name(conn, d.type_code)) for d in description)
            while not truncated:
                # statement_timeout applies per FETCH; enforce the overall budget across fetches too
                if time.monotonic() > deadline:
                    raise WarehouseQueryTimeout()
                batch = qc.fetchmany(max(1, min(1000, row_cap + 1 - len(rows))))
                if not batch:
                    break
                for r in batch:
                    if len(rows) >= row_cap:
                        truncated = True
                        break
                    size = _row_bytes(r)
                    if nbytes + size > byte_cap:
                        truncated = True
                        break
                    rows.append(tuple(r))
                    nbytes += size
        return ResultSet(
            columns=columns,
            rows=tuple(rows),
            row_count=len(rows),
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except pg_errors.QueryCanceled as exc:
        raise WarehouseQueryTimeout() from exc
    except (pg_errors.InsufficientPrivilege, pg_errors.ReadOnlySqlTransaction) as exc:
        raise WarehouseQueryDenied(exc.diag.message_primary or None) from exc
    except psycopg.OperationalError as exc:
        raise WarehouseUnavailable() from exc
    except psycopg.Error as exc:
        raise WarehouseQueryFailed(exc.diag.message_primary or type(exc).__name__) from exc
    finally:
        try:
            conn.rollback()
        except psycopg.Error:  # pragma: no cover - connection already broken
            pass
        conn.close()


def preview_rows(org_id: UUID | str, table: str, column_names: Sequence[str], *, limit: int = 100) -> ResultSet:
    """First `limit` rows of a dataset version table, read through the reader role."""
    cols = ", ".join(f'"{naming.check_ident(c)}"' for c in column_names) or "*"
    # identifiers are validated by check_ident (lower-case [a-z0-9_] only); the limit is a bound parameter
    statement = f'SELECT {cols} FROM "{naming.check_ident(table)}" LIMIT %s'  # noqa: S608
    return execute_readonly_sql(org_id, statement, [int(limit)], row_cap=int(limit))
