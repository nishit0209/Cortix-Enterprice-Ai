"""A scripted stand-in for a psycopg connection (pure tests of the PostgreSQL connector)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

from psycopg import sql


def render(query: Any) -> str:
    if isinstance(query, sql.Composable):
        return query.as_string(None)
    return str(query)


class FakeCursor:
    def __init__(self, conn: FakeConnection, name: str | None = None):
        self.conn = conn
        self.name = name
        self.itersize = 100
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        text = render(query)
        self.conn.executed.append((text, list(params) if params is not None else None, self.name))
        self._rows = list(self.conn.responder(text, params, self.name) or [])
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows


class FakeConnection:
    def __init__(self, responder: Callable[[str, Any, str | None], list[tuple]]):
        self.responder = responder
        self.executed: list[tuple[str, Any, str | None]] = []
        self.read_only = False
        self.closed = False

    def cursor(self, name: str | None = None):
        return FakeCursor(self, name)

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


CLEAN_ROLE = (False, False, False, False, False)


def catalog_responder(*, role=CLEAN_ROLE, dangerous=(), db_create=False, schema_create=(), writable=(),
                      tables=(), columns=(), rows=(), max_value=None):
    """Answers the connector's catalog/privilege queries. tables: [(oid, schema, name, relkind, reltuples)],
    columns: [(relid, name, type, nullable, attnum)], rows: data rows for the named read cursor."""

    def respond(text: str, params, name):
        if name == "nexora_read":
            return list(rows)
        if "FROM pg_roles WHERE rolname = current_user" in text:
            return [role] if role is not None else []
        if "pg_has_role" in text:
            return [(r,) for r in dangerous]
        if "has_database_privilege" in text:
            return [(db_create,)]
        if "has_schema_privilege(n.oid, 'CREATE')" in text:
            return [(s,) for s in schema_create]
        if "has_table_privilege(c.oid, 'INSERT')" in text:
            return [(t,) for t in writable]
        if "current_setting('server_version')" in text:
            return [("17.2", "nexora_ro", "shop")]
        if "SELECT c.oid FROM pg_class" in text:
            schema, table = params
            return [(t[0],) for t in tables if t[1] == schema and t[2] == table]
        if "SELECT c.oid, n.nspname" in text:
            return [t for t in tables]
        if "a.attrelid = ANY" in text:
            wanted = set(params[0])
            return [c for c in columns if c[0] in wanted]
        if "WHERE a.attrelid = %s" in text:
            return [(c[1], c[2], c[3], c[4]) for c in columns if c[0] == params[0]]
        if text.startswith("SELECT max("):
            return [(max_value,)]
        return []

    return respond


def public_resolver(ip: str = "52.10.20.30", calls: list | None = None):
    def resolve(host, port, *a):
        if calls is not None:
            calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return resolve
