"""PostgresConnector against the REAL local PostgreSQL (the test cluster plays the "external" database).

ALLOW_PRIVATE_DB_HOSTS is true in test settings, so localhost is reachable. Roles used:
  * nexora_app  -> has DML on tables: MUST be refused as not read-only;
  * wh_executor -> no privileges of its own: passes the read-only check; a fixture schema grants it SELECT.
"""

from __future__ import annotations

import environ
import psycopg
import pytest
from django.conf import settings
from django.db import connection

from apps.connectors.exceptions import ColumnNotFound, TableNotFound
from apps.connectors.postgres import PostgresConnector

pytestmark = pytest.mark.django_db

FIXTURE_SCHEMA = "ext_fixture_dp"


def _url(var: str, default: str) -> dict:
    import os

    return environ.Env.db_url_config(os.environ.get(var, default))


def _host_port() -> tuple[str, int]:
    s = connection.settings_dict
    return s["HOST"] or "localhost", int(s["PORT"] or 5432)


def _executor() -> tuple[str, str]:
    cfg = environ.Env.db_url_config(settings.WH_DATABASE_URL_EXECUTOR)
    return cfg["USER"], cfg["PASSWORD"]


def _connector(user: str, password: str, schema: str | None = None) -> PostgresConnector:
    host, port = _host_port()
    return PostgresConnector({"host": host, "port": port, "dbname": connection.settings_dict["NAME"],
                              "sslmode": "disable", "schema": schema}, username=user, password=password)


@pytest.fixture
def fixture_schema():
    """Create a schema + table as the owner role, readable by wh_executor only."""
    mig = _url("DATABASE_URL_MIGRATOR", "postgres://nexora_migrator:nexora_migrator_dev@localhost:5433/nexora_app")
    host, port = _host_port()
    exec_user, _ = _executor()
    params = {"host": host, "port": port, "dbname": connection.settings_dict["NAME"], "user": mig["USER"],
              "password": mig["PASSWORD"], "options": "-c role=nexora_owner", "connect_timeout": 5}
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{FIXTURE_SCHEMA}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{FIXTURE_SCHEMA}"')
        conn.execute(f'CREATE TABLE "{FIXTURE_SCHEMA}".orders (id bigint PRIMARY KEY, city text, total numeric(12,2), '
                     f'placed timestamptz)')
        conn.execute(f'INSERT INTO "{FIXTURE_SCHEMA}".orders SELECT g, (ARRAY[\'Pune\',\'Delhi\'])[1 + g % 2], '
                     f'g * 10.5, timestamptz \'2024-01-01 00:00+00\' + g * interval \'1 day\' FROM generate_series(1, 25) g')
        conn.execute(f'ANALYZE "{FIXTURE_SCHEMA}".orders')
        conn.execute(f'GRANT USAGE ON SCHEMA "{FIXTURE_SCHEMA}" TO "{exec_user}"')
        conn.execute(f'GRANT SELECT ON "{FIXTURE_SCHEMA}".orders TO "{exec_user}"')
    yield FIXTURE_SCHEMA
    with psycopg.connect(**params, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{FIXTURE_SCHEMA}" CASCADE')


def test_app_role_with_write_privileges_is_refused():
    s = connection.settings_dict
    result = _connector(s["USER"], s["PASSWORD"]).test()
    assert not result.ok and result.code == "connector_role_not_readonly"
    assert any(p.startswith("write:public.") for p in result.readonly.problems)


def test_readonly_role_passes_and_reads(fixture_schema):
    user, pw = _executor()
    connector = _connector(user, pw, schema=fixture_schema)
    result = connector.test()
    assert result.ok, result.as_dict()
    assert result.current_user == user and result.readonly.ok
    tables = connector.introspect()
    assert [(t.schema, t.name) for t in tables] == [(fixture_schema, "orders")]
    assert [c.name for c in tables[0].columns] == ["id", "city", "total", "placed"]
    batches = list(connector.read_batches("orders", ["id", "total"], batch_rows=10))
    assert [len(b.rows) for b in batches] == [10, 10, 5]
    inc = list(connector.read_batches("orders", ["id"], cursor_column="id", after=20))
    assert [r[0] for b in inc for r in b.rows] == [21, 22, 23, 24, 25]
    assert connector.incremental_cursor("orders", "id").value == 25
    with pytest.raises(TableNotFound):
        list(connector.read_batches("pg_authid", schema=fixture_schema))
    with pytest.raises(ColumnNotFound):
        list(connector.read_batches("orders", ['id" FROM pg_authid --']))


def test_sessions_are_read_only(fixture_schema):
    user, pw = _executor()
    connector = _connector(user, pw, schema=fixture_schema)
    conn = connector.connect()
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE t_probe (x int)")
        conn.rollback()
        assert conn.execute("SHOW application_name").fetchone()[0] == "nexora"
    finally:
        conn.close()


def test_wrong_password_maps_to_auth_failed():
    user, _pw = _executor()
    result = _connector(user, "wrong-synthetic-password").test()
    assert not result.ok and result.code == "connector_auth_failed"
    assert "wrong-synthetic-password" not in result.message
