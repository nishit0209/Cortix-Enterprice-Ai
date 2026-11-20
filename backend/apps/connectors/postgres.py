"""External PostgreSQL connector (psycopg 3).

Security properties (DATA_ENGINEERING_DESIGN §2, SECURITY_ARCHITECTURE §8):
  * SSRF: the host is resolved once by `ssrf.resolve_and_check`; libpq gets `hostaddr=<pinned ip>` + `host=<name>`
    (SNI / certificate verification) so DNS rebinding cannot redirect the connection.
  * TLS: `sslmode=require` by default; `verify-full`/`verify-ca` use the system CA store. `disable`/`prefer`
    are accepted only when ALLOW_PRIVATE_DB_HOSTS (dev).
  * Every session is read-only with timeouts (`default_transaction_read_only`, `statement_timeout`, ...), named
    `application_name=nexora`, never reads ~/.pgpass, and only ever receives these whitelisted libpq parameters.
  * The role must be read-only: not superuser / BYPASSRLS / CREATEROLE / CREATEDB / REPLICATION, no membership in
    pg_write_server_files / pg_execute_server_program / pg_read_server_files / pg_write_all_data, no CREATE on the
    database or the allowlisted schema(s), and no INSERT/UPDATE/DELETE/TRUNCATE on any table there.
  * Identifiers are always composed with `psycopg.sql.Identifier`; the table and columns are verified against
    the catalog (SELECT privilege) before any read.
The password is held only in memory for the lifetime of the connector object and never logged.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any, ClassVar, Literal

import psycopg
from django.conf import settings
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import ssrf
from .base import (
    BaseConnector,
    Batch,
    ColumnInfo,
    CursorState,
    ReadonlyCheck,
    TableInfo,
    TestResult,
    register_connector,
)
from .exceptions import (
    AuthenticationFailed,
    ColumnNotFound,
    ConnectionFailed,
    ConnectorError,
    ImportTooLarge,
    QueryFailed,
    QueryTimeout,
    TableNotFound,
    TlsFailed,
)

log = logging.getLogger(__name__)

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
SYSTEM_SCHEMA_SQL = "n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') AND n.nspname NOT LIKE 'pg\\_%%'"
DANGEROUS_ROLES = ("pg_write_server_files", "pg_execute_server_program", "pg_read_server_files", "pg_write_all_data")
RELKINDS = {"r": "table", "p": "partitioned_table", "v": "view", "m": "materialized_view", "f": "foreign_table"}
MAX_TABLES = 500
MAX_COLUMNS_PER_TABLE = 1000
DEV_SSLMODES = ("disable", "allow", "prefer")


class PostgresConfig(BaseModel):
    """Non-secret configuration stored in DataSource.config. Unknown keys are rejected (no libpq passthrough)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(min_length=1, max_length=63)
    sslmode: Literal["require", "verify-ca", "verify-full", "prefer", "allow", "disable"] = "require"
    schema_name: str | None = Field(default=None, alias="schema")

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return ssrf.normalize_host(v)

    @field_validator("dbname")
    @classmethod
    def _dbname(cls, v: str) -> str:
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("The database name contains control characters.")
        return v

    @field_validator("schema_name")
    @classmethod
    def _schema(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not IDENT_RE.match(v):
            raise ValueError("The schema must be a plain identifier.")
        return v

    @field_validator("sslmode")
    @classmethod
    def _sslmode(cls, v: str) -> str:
        if v in DEV_SSLMODES and not getattr(settings, "ALLOW_PRIVATE_DB_HOSTS", False):
            raise ValueError("Unencrypted connections are not allowed; use require, verify-ca or verify-full.")
        return v

    def public(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "dbname": self.dbname, "sslmode": self.sslmode,
                "schema": self.schema_name}


def session_options() -> str:
    timeout_ms = int(getattr(settings, "CONNECTOR_STATEMENT_TIMEOUT_S", 30)) * 1000
    return (f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms} -c lock_timeout=5000 "
            f"-c idle_in_transaction_session_timeout=120000")


def evaluate_readonly(facts: dict[str, Any]) -> ReadonlyCheck:
    """Pure decision over catalog facts gathered by `PostgresConnector.readonly_facts`."""
    problems: list[str] = []
    role = facts.get("role") or {}
    if not role:
        problems.append("role_unknown")
    for attr, key in (("rolsuper", "superuser"), ("rolbypassrls", "bypassrls"), ("rolcreaterole", "createrole"),
                      ("rolcreatedb", "createdb"), ("rolreplication", "replication")):
        if role.get(attr):
            problems.append(key)
    problems.extend(f"member:{r}" for r in sorted(facts.get("dangerous_roles") or ()))
    if facts.get("db_create"):
        problems.append("create:database")
    problems.extend(f"create:schema:{s}" for s in sorted(facts.get("schema_create") or ()))
    problems.extend(f"write:{t}" for t in facts.get("writable_tables") or ())
    return ReadonlyCheck(ok=not problems, problems=tuple(problems))


def _scrub(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, "***")
    return text[:300]


def map_error(exc: BaseException, secret: str | None = None) -> ConnectorError:
    """Translate a psycopg error into a stable connector error (the server text is never shown verbatim)."""
    if isinstance(exc, ConnectorError):
        return exc
    msg = str(exc).lower()
    if isinstance(exc, psycopg.errors.QueryCanceled) or "statement timeout" in msg:
        return QueryTimeout()
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return QueryFailed("The connector role lacks a privilege needed for this operation.",
                           code="connector_permission_denied")
    if isinstance(exc, psycopg.OperationalError):
        if "password authentication failed" in msg or "authentication failed" in msg or "role \"" in msg \
                and "does not exist" in msg:
            return AuthenticationFailed()
        if "pg_hba.conf" in msg:
            return AuthenticationFailed("The database does not accept connections from Nexora for this user.")
        if "ssl" in msg or "certificate" in msg or "tls" in msg:
            return TlsFailed()
        if "database \"" in msg and "does not exist" in msg:
            return ConnectionFailed("The database does not exist.", code="connector_database_not_found")
        if "timeout" in msg or "timed out" in msg:
            return ConnectionFailed("The connection to the database timed out.", code="connector_timeout")
        return ConnectionFailed()
    if isinstance(exc, psycopg.Error):
        return QueryFailed()
    return ConnectionFailed()


@register_connector
class PostgresConnector(BaseConnector):
    type: ClassVar[str] = "postgresql"
    config_schema: ClassVar[type[BaseModel]] = PostgresConfig

    def __init__(self, config: PostgresConfig | dict, *, username: str, password: str,
                 pinned_ip: str | None = None, connect_fn: Callable[..., Any] | None = None,
                 resolver: Callable[..., Any] | None = None):
        self.config = config if isinstance(config, PostgresConfig) else PostgresConfig.model_validate(config)
        self.username = username
        self._password = password
        self._pinned_ip = pinned_ip
        self._connect_fn = connect_fn or psycopg.connect
        self._resolver = resolver
        self.resolved: ssrf.ResolvedHost | None = None

    def __repr__(self) -> str:  # never expose the password
        return f"<PostgresConnector {self.username}@{self.config.host}:{self.config.port}/{self.config.dbname}>"

    # -- connection -----------------------------------------------------------------------------
    def resolve(self) -> ssrf.ResolvedHost:
        """SSRF check + pin. Re-validates the stored pin: a previously pinned IP must still be allowed."""
        kwargs = {"resolver": self._resolver} if self._resolver else {}
        self.resolved = ssrf.resolve_and_check(self.config.host, self.config.port, **kwargs)
        return self.resolved

    def connect_kwargs(self, pinned: ssrf.ResolvedHost) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": pinned.host,
            "hostaddr": pinned.ip,
            "port": pinned.port,
            "dbname": self.config.dbname,
            "user": self.username,
            "password": self._password,
            "sslmode": self.config.sslmode,
            "connect_timeout": int(getattr(settings, "CONNECTOR_CONNECT_TIMEOUT_S", 10)),
            "application_name": "nexora",
            "options": session_options(),
            "gssencmode": "disable",
            "passfile": os.devnull,
            "target_session_attrs": "any",
        }
        if self.config.sslmode in ("verify-ca", "verify-full"):
            kwargs["sslrootcert"] = "system"
        return kwargs

    def connect(self):
        pinned = self.resolve()
        try:
            conn = self._connect_fn(**self.connect_kwargs(pinned))
        except psycopg.Error as exc:
            log.info("connector connect failed: %s", _scrub(str(exc), self._password))
            raise map_error(exc, self._password) from None
        conn.read_only = True
        return conn

    # -- checks ---------------------------------------------------------------------------------
    def _schema_filter(self) -> tuple[sql.Composable, list[Any]]:
        if self.config.schema_name:
            return sql.SQL("n.nspname = %s"), [self.config.schema_name]
        return sql.SQL(SYSTEM_SCHEMA_SQL), []  # always executed WITH a params list, so "%%" is a literal "%"

    def readonly_facts(self, conn) -> dict[str, Any]:
        cur = conn.cursor()
        cur.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
        role = dict(zip(("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb", "rolreplication"), row,
                        strict=True)) if row else {}
        cur.execute(
            "SELECT r.rolname FROM pg_roles r WHERE r.rolname = ANY(%s) AND pg_has_role(current_user, r.oid, 'MEMBER')",
            [list(DANGEROUS_ROLES)],
        )
        dangerous = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT has_database_privilege(current_database(), 'CREATE')")
        db_create = bool(cur.fetchone()[0])
        cond, params = self._schema_filter()
        cur.execute(sql.SQL(
            "SELECT n.nspname FROM pg_namespace n WHERE {cond} AND has_schema_privilege(n.oid, 'CREATE') ORDER BY 1"
        ).format(cond=cond), params)
        schema_create = [r[0] for r in cur.fetchall()]
        cur.execute(sql.SQL(
            "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND {cond} AND ("
            "has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE') OR "
            "has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE')) ORDER BY 1 LIMIT 20"
        ).format(cond=cond), params)
        writable = [r[0] for r in cur.fetchall()]
        return {"role": role, "dangerous_roles": dangerous, "db_create": db_create, "schema_create": schema_create,
                "writable_tables": writable}

    def test(self) -> TestResult:
        started = time.monotonic()
        try:
            conn = self.connect()
        except ConnectorError as exc:
            return TestResult(ok=False, code=exc.get_codes() if isinstance(exc.get_codes(), str) else "connector_error",
                              message=str(exc.detail), resolved_ip=self.resolved.ip if self.resolved else "",
                              latency_ms=int((time.monotonic() - started) * 1000))
        try:
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT current_setting('server_version'), current_user, current_database()")
                version, user, database = cur.fetchone()
                readonly = evaluate_readonly(self.readonly_facts(conn))
                conn.rollback()
        except psycopg.Error as exc:
            err = map_error(exc, self._password)
            return TestResult(ok=False, code=str(err.get_codes()), message=str(err.detail),
                              resolved_ip=self.resolved.ip if self.resolved else "",
                              latency_ms=int((time.monotonic() - started) * 1000))
        latency = int((time.monotonic() - started) * 1000)
        if not readonly.ok:
            return TestResult(ok=False, server_version=str(version), current_user=str(user), database=str(database),
                              resolved_ip=self.resolved.ip, readonly=readonly, latency_ms=latency,
                              code="connector_role_not_readonly",
                              message="The database role has write or administrative privileges; use a read-only role.")
        return TestResult(ok=True, server_version=str(version), current_user=str(user), database=str(database),
                          resolved_ip=self.resolved.ip, readonly=readonly, latency_ms=latency,
                          message="Connected; the role is read-only.")

    def assert_readonly(self, conn) -> None:
        check = evaluate_readonly(self.readonly_facts(conn))
        if not check.ok:
            raise _not_readonly(check)

    # -- catalog --------------------------------------------------------------------------------
    def introspect(self) -> list[TableInfo]:
        with self._session() as conn:
            cur = conn.cursor()
            cond, params = self._schema_filter()
            cur.execute(sql.SQL(
                "SELECT c.oid, n.nspname, c.relname, c.relkind, c.reltuples::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {cond} "
                "AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT') "
                "ORDER BY n.nspname, c.relname LIMIT %s"
            ).format(cond=cond), [*params, MAX_TABLES])
            rels = cur.fetchall()
            columns: dict[int, list[ColumnInfo]] = {}
            if rels:
                cur.execute(
                    "SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.attnum "
                    "FROM pg_attribute a WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped "
                    "ORDER BY a.attrelid, a.attnum",
                    [[r[0] for r in rels]],
                )
                for relid, name, typ, nullable, num in cur.fetchall():
                    bucket = columns.setdefault(relid, [])
                    if len(bucket) < MAX_COLUMNS_PER_TABLE:
                        bucket.append(ColumnInfo(name=name, type=typ, nullable=bool(nullable), ordinal=int(num)))
            conn.rollback()
        return [
            TableInfo(schema=schema, name=name, kind=RELKINDS.get(kind, "table"),
                      estimated_rows=int(est) if est is not None and est >= 0 else None,
                      columns=tuple(columns.get(oid, ())))
            for oid, schema, name, kind, est in rels
        ]

    def _session(self):
        conn = self.connect()
        return _Session(conn, self)

    def table_columns(self, conn, schema: str, table: str) -> list[ColumnInfo]:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')",
            [schema, table],
        )
        row = cur.fetchone()
        if row is None:
            raise TableNotFound()
        cur.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, a.attnum FROM pg_attribute a "
            "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            [row[0]],
        )
        return [ColumnInfo(name=n, type=t, nullable=bool(nl), ordinal=int(o)) for n, t, nl, o in cur.fetchall()]

    def _target(self, table: str, schema: str | None) -> tuple[str, str]:
        schema = schema or self.config.schema_name or "public"
        if self.config.schema_name and schema != self.config.schema_name:
            raise TableNotFound("Only tables in the configured schema can be read.")
        if not table or len(table) > 63:
            raise TableNotFound()
        return schema, table

    # -- reads ----------------------------------------------------------------------------------
    def read_batches(self, table: str, columns: Sequence[str] | None = None, batch_rows: int = 10_000, *,
                     schema: str | None = None, cursor_column: str | None = None, after: Any = None,
                     max_rows: int | None = None) -> Iterator[Batch]:
        schema, table = self._target(table, schema)
        batch_rows = max(1, min(int(batch_rows), 100_000))
        with self._session() as conn:
            self.assert_readonly(conn)  # re-verified on every read
            available = self.table_columns(conn, schema, table)
            by_name = {c.name: c for c in available}
            if columns:
                missing = [c for c in columns if c not in by_name]
                if missing:
                    raise ColumnNotFound(f"Unknown columns: {', '.join(missing[:10])}.")
                selected = [by_name[c] for c in dict.fromkeys(columns)]
            else:
                selected = available
            if not selected:
                raise ColumnNotFound("The table has no readable columns.")
            if cursor_column and cursor_column not in by_name:
                raise ColumnNotFound(f"Unknown cursor column: {cursor_column}.")
            query = sql.SQL("SELECT {cols} FROM {schema}.{table}").format(
                cols=sql.SQL(", ").join(sql.Identifier(c.name) for c in selected),
                schema=sql.Identifier(schema), table=sql.Identifier(table),
            )
            params: list[Any] = []
            if cursor_column:
                if after is not None:
                    query += sql.SQL(" WHERE {c} > %s").format(c=sql.Identifier(cursor_column))
                    params.append(after)
                query += sql.SQL(" ORDER BY {c}").format(c=sql.Identifier(cursor_column))
            if max_rows is not None:
                query += sql.SQL(" LIMIT %s")
                params.append(int(max_rows) + 1)
            meta = tuple(selected)
            seen = 0
            try:
                with conn.cursor(name="nexora_read") as cur:
                    cur.itersize = batch_rows
                    cur.execute(query, params)
                    while True:
                        rows = cur.fetchmany(batch_rows)
                        if not rows:
                            break
                        seen += len(rows)
                        if max_rows is not None and seen > max_rows:
                            raise ImportTooLarge(f"The table has more than {max_rows} rows.")
                        yield Batch(columns=meta, rows=rows)
                if seen == 0:
                    yield Batch(columns=meta, rows=[])  # empty table: callers still learn the header
            except psycopg.Error as exc:
                raise map_error(exc, self._password) from None
            conn.rollback()

    def incremental_cursor(self, table: str, cursor_column: str, *, schema: str | None = None) -> CursorState:
        schema, table = self._target(table, schema)
        with self._session() as conn:
            names = {c.name for c in self.table_columns(conn, schema, table)}
            if cursor_column not in names:
                raise ColumnNotFound(f"Unknown cursor column: {cursor_column}.")
            cur = conn.cursor()
            cur.execute(sql.SQL("SELECT max({c}) FROM {s}.{t}").format(
                c=sql.Identifier(cursor_column), s=sql.Identifier(schema), t=sql.Identifier(table)))
            value = cur.fetchone()[0]
            conn.rollback()
        return CursorState(column=cursor_column, value=value)


def _not_readonly(check: ReadonlyCheck) -> ConnectorError:
    from .exceptions import RoleNotReadOnly

    return RoleNotReadOnly(extra={"problems": list(check.problems)})


class _Session:
    """Context manager around a connection that maps psycopg errors and always closes."""

    def __init__(self, conn, connector: PostgresConnector):
        self.conn = conn
        self.connector = connector

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001,S110 - closing a broken connection must not mask the real error
            pass
        if exc is not None and isinstance(exc, psycopg.Error):
            raise map_error(exc, self.connector._password) from None
        return False
