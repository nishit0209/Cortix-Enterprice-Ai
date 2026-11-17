"""Raw-SQL helpers for tenancy/RLS verification.

The tenant setting name always comes from `TENANT_GUC` (never spelled out here).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from apps.core.tenancy.constants import TENANT_GUC


def db_tenant_value(using: str = "default") -> str | None:
    """Current value of the tenant setting on the connection ('' / None when unset)."""
    from django.db import connections

    with connections[using].cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
        return cur.fetchone()[0]


def clear_db_tenant(using: str = "default") -> None:
    """Reset the transaction-local tenant setting to '' (RLS then matches zero rows).

    Needed inside pytest-django's wrapping transaction: a transaction-local setting made inside a
    nested `tenant_context` survives the savepoint release until the OUTER transaction ends.
    """
    from django.db import connections

    conn = connections[using]
    if not conn.in_atomic_block:
        return  # autocommit: the setting already expired with its transaction
    with conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, '', true)", [TENANT_GUC])


@contextmanager
def db_tenant(org_id: UUID | str | None, using: str = "default") -> Iterator[None]:
    """Set ONLY the database tenant setting (no ContextVar, no TenantManager) — for raw-SQL RLS probes."""
    from django.db import connections, transaction

    with transaction.atomic(using=using):
        with connections[using].cursor() as cur:
            cur.execute(
                "SELECT set_config(%s, %s, true)", [TENANT_GUC, "" if org_id is None else str(org_id)]
            )
        try:
            yield
        finally:
            with connections[using].cursor() as cur:
                cur.execute("SELECT set_config(%s, '', true)", [TENANT_GUC])


def tenant_tables(using: str = "default") -> dict[str, str]:
    """{table: tenant column} for every public table with an org_id column, plus orgs_organization (id)."""
    from django.db import connections

    with connections[using].cursor() as cur:
        cur.execute(
            """
            SELECT c.table_name FROM information_schema.columns c
            JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public' AND c.column_name = 'org_id' AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name
            """
        )
        tables = {r[0]: "org_id" for r in cur.fetchall()}
    tables["orgs_organization"] = "id"
    return tables


@dataclass(frozen=True)
class RlsStatus:
    table: str
    enabled: bool
    forced: bool
    policies: dict[str, tuple[str | None, str | None]]  # policy name -> (USING, WITH CHECK)

    @property
    def tenant_policy(self) -> tuple[str | None, str | None] | None:
        return self.policies.get("tenant_isolation")


def rls_status(table: str, using: str = "default") -> RlsStatus:
    from django.db import connections

    with connections[using].cursor() as cur:
        cur.execute(
            """
            SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = %s
            """,
            [table],
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"table {table} does not exist")
        cur.execute(
            "SELECT policyname, qual, with_check FROM pg_policies WHERE schemaname = 'public' AND tablename = %s",
            [table],
        )
        policies = {name: (qual, check) for name, qual, check in cur.fetchall()}
    return RlsStatus(table=table, enabled=bool(row[0]), forced=bool(row[1]), policies=policies)


def table_columns(table: str, using: str = "default") -> dict[str, str]:
    from django.db import connections

    with connections[using].cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            [table],
        )
        return dict(cur.fetchall())


def sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a (Django-wrapped) psycopg error, e.g. '42501' for RLS / privilege violations."""
    for e in (exc, exc.__cause__, exc.__context__):
        code = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        if code:
            return code
    return None


def app_connection_params(using: str = "default") -> dict:
    """psycopg connect kwargs for a NEW, independent connection with the same role and (test) database."""
    from django.db import connections

    s = connections[using].settings_dict
    params = {
        "dbname": s["NAME"],
        "user": s["USER"],
        "password": s["PASSWORD"],
        "host": s["HOST"] or "localhost",
    }
    if s.get("PORT"):
        params["port"] = int(s["PORT"])
    return params


@contextmanager
def independent_connection(using: str = "default", autocommit: bool = False):
    """A raw psycopg connection that shares nothing (session, transaction) with Django's connection."""
    import psycopg

    conn = psycopg.connect(**app_connection_params(using), autocommit=autocommit, connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()
