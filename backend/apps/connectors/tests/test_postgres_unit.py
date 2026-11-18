"""PostgreSQL connector (pure): config hardening, IP pinning, read-only role verification, reads, error mapping."""

import logging
import os
import socket

import psycopg
import pytest

from apps.connectors.exceptions import (
    AuthenticationFailed,
    ColumnNotFound,
    HostForbidden,
    ImportTooLarge,
    RoleNotReadOnly,
    TableNotFound,
)
from apps.connectors.postgres import PostgresConfig, PostgresConnector, evaluate_readonly, map_error

from .fakepg import CLEAN_ROLE, FakeConnection, catalog_responder, public_resolver

SECRET = "Synthetic-Canary-Pw-9f3c"  # synthetic test secret
CONFIG = {"host": "db.example.com", "port": 5432, "dbname": "shop", "sslmode": "require", "schema": "sales"}
TABLES = [(101, "sales", "orders", "r", 1200), (102, "sales", "v_daily", "v", -1)]
COLUMNS = [(101, "id", "bigint", False, 1), (101, "total", "numeric(12,2)", True, 2), (101, "note", "text", True, 3),
           (102, "day", "date", True, 1)]


def make(responder=None, *, config=CONFIG, resolver=None, captured=None):
    captured = captured if captured is not None else []

    def connect(**kwargs):
        captured.append(kwargs)
        return FakeConnection(responder or catalog_responder(tables=TABLES, columns=COLUMNS))

    return PostgresConnector(config, username="nexora_ro", password=SECRET, connect_fn=connect,
                             resolver=resolver or public_resolver()), captured


# --- config ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("extra", [{"options": "-c role=postgres"}, {"sslkey": "/etc/passwd"}, {"passfile": "/x"},
                                   {"service": "prod"}, {"hostaddr": "10.0.0.1"}])
def test_config_rejects_libpq_passthrough(extra):
    with pytest.raises(Exception):
        PostgresConfig.model_validate({**CONFIG, **extra})


def test_config_rules(settings):
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    with pytest.raises(Exception):
        PostgresConfig.model_validate({**CONFIG, "sslmode": "disable"})
    with pytest.raises(Exception):
        PostgresConfig.model_validate({**CONFIG, "schema": "sales; drop table x"})
    with pytest.raises(Exception):
        PostgresConfig.model_validate({**CONFIG, "port": 70000})
    with pytest.raises(Exception):
        PostgresConfig.model_validate({**CONFIG, "host": "http://db.example.com"})
    cfg = PostgresConfig.model_validate({**CONFIG, "host": "DB.Example.com"})
    assert cfg.public() == {**CONFIG, "host": "db.example.com"}
    settings.ALLOW_PRIVATE_DB_HOSTS = True
    assert PostgresConfig.model_validate({**CONFIG, "sslmode": "disable"}).sslmode == "disable"


# --- connection: SSRF pinning --------------------------------------------------------------------------------
def test_connect_pins_resolved_ip_and_hardens_session():
    calls = []
    connector, captured = make(resolver=public_resolver("52.10.20.30", calls))
    connector.connect()
    (kw,) = captured
    assert kw["host"] == "db.example.com" and kw["hostaddr"] == "52.10.20.30" and calls == ["db.example.com"]
    assert kw["passfile"] == os.devnull and kw["application_name"] == "nexora" and kw["gssencmode"] == "disable"
    assert "default_transaction_read_only=on" in kw["options"] and "statement_timeout=" in kw["options"]
    assert kw["sslmode"] == "require" and "sslrootcert" not in kw
    assert set(kw) == {"host", "hostaddr", "port", "dbname", "user", "password", "sslmode", "connect_timeout",
                       "application_name", "options", "gssencmode", "passfile", "target_session_attrs"}


def test_verify_full_uses_system_ca_store():
    connector, captured = make(config={**CONFIG, "sslmode": "verify-full"})
    connector.connect()
    assert captured[0]["sslrootcert"] == "system" and captured[0]["sslmode"] == "verify-full"


def test_dns_rebinding_cannot_redirect_a_connection(settings):
    """First lookup is public (pinned); a rebinding answer on the next lookup is refused before connecting."""
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    answers = [["52.10.20.30"], ["127.0.0.1"]]

    def rebinding(host, port, *a):
        ip = answers.pop(0)[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    connector, captured = make(resolver=rebinding)
    connector.connect()
    assert captured[0]["hostaddr"] == "52.10.20.30"  # libpq connects to the checked IP, never re-resolves
    with pytest.raises(HostForbidden):
        connector.connect()
    assert len(captured) == 1


def test_private_target_refused_before_connecting(settings):
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    connector, captured = make(resolver=public_resolver("10.0.0.8"))
    result = connector.test()
    assert not result.ok and result.code == "connector_host_forbidden" and captured == []


# --- read-only verification --------------------------------------------------------------------------------
def test_evaluate_readonly_clean():
    assert evaluate_readonly({"role": dict(zip(("rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb",
                                                 "rolreplication"), CLEAN_ROLE, strict=True))}).ok


@pytest.mark.parametrize("facts,problem", [
    ({"role": {"rolsuper": True}}, "superuser"),
    ({"role": {"rolbypassrls": True}}, "bypassrls"),
    ({"role": {"rolcreaterole": True}}, "createrole"),
    ({"role": {"rolcreatedb": True}}, "createdb"),
    ({"role": {"rolreplication": True}}, "replication"),
    ({"role": {"rolsuper": False}, "dangerous_roles": ["pg_write_server_files"]}, "member:pg_write_server_files"),
    ({"role": {"rolsuper": False}, "dangerous_roles": ["pg_write_all_data"]}, "member:pg_write_all_data"),
    ({"role": {"rolsuper": False}, "db_create": True}, "create:database"),
    ({"role": {"rolsuper": False}, "schema_create": ["sales"]}, "create:schema:sales"),
    ({"role": {"rolsuper": False}, "writable_tables": ["sales.orders"]}, "write:sales.orders"),
    ({"role": {}}, "role_unknown"),
])
def test_evaluate_readonly_problems(facts, problem):
    check = evaluate_readonly(facts)
    assert not check.ok and problem in check.problems


def test_test_reports_not_readonly_role():
    connector, _ = make(catalog_responder(role=(False, False, False, False, False), writable=["sales.orders"],
                                          schema_create=["sales"]))
    result = connector.test()
    assert not result.ok and result.code == "connector_role_not_readonly"
    assert set(result.readonly.problems) == {"write:sales.orders", "create:schema:sales"}
    assert result.current_user == "nexora_ro" and result.resolved_ip == "52.10.20.30"


def test_test_passes_for_readonly_role():
    connector, _ = make()
    result = connector.test()
    assert result.ok and result.readonly.ok and result.server_version == "17.2"
    assert SECRET not in repr(result.as_dict()) and SECRET not in repr(connector)


def test_privilege_queries_use_the_configured_schema():
    seen = []
    base = catalog_responder()

    def spy(text, params, name):
        seen.append((text, params))
        return base(text, params, name)

    connector, _ = make(spy)
    connector.test()
    schema_queries = [p for t, p in seen if "has_schema_privilege(n.oid, 'CREATE')" in t or "'TRUNCATE'" in t]
    assert schema_queries and all(p == ["sales"] for p in schema_queries)


def test_without_schema_all_user_schemas_are_checked():
    seen = []
    base = catalog_responder()

    def spy(text, params, name):
        seen.append(text)
        return base(text, params, name)

    connector, _ = make(spy, config={**CONFIG, "schema": None})
    connector.test()
    assert any("n.nspname NOT IN ('pg_catalog'" in t for t in seen)


# --- catalog & reads ----------------------------------------------------------------------------------------------
def test_introspect_builds_tables_and_columns():
    connector, _ = make()
    tables = connector.introspect()
    assert [(t.schema, t.name, t.kind, t.estimated_rows) for t in tables] == [
        ("sales", "orders", "table", 1200), ("sales", "v_daily", "view", None)]
    assert [(c.name, c.type) for c in tables[0].columns] == [("id", "bigint"), ("total", "numeric(12,2)"),
                                                             ("note", "text")]


def test_read_batches_quotes_identifiers_and_streams():
    rows = [(i, f"{i}.50", None) for i in range(5)]
    connector, _ = make(catalog_responder(tables=TABLES, columns=COLUMNS, rows=rows))
    conn_holder = []
    original = connector._connect_fn

    def capture(**kw):
        conn = original(**kw)
        conn_holder.append(conn)
        return conn

    connector._connect_fn = capture
    batches = list(connector.read_batches("orders", ["id", "total", "note"], batch_rows=2))
    assert [len(b.rows) for b in batches] == [2, 2, 1]
    assert [c.name for c in batches[0].columns] == ["id", "total", "note"]
    read_sql = [s for s, _p, name in conn_holder[0].executed if name == "nexora_read"][0]
    assert read_sql == 'SELECT "id", "total", "note" FROM "sales"."orders"'
    assert conn_holder[0].read_only is True and conn_holder[0].closed


def test_read_batches_incremental_and_limit():
    connector, _ = make(catalog_responder(tables=TABLES, columns=COLUMNS, rows=[(1,), (2,)]))
    holder = []
    original = connector._connect_fn
    connector._connect_fn = lambda **kw: holder.append(original(**kw)) or holder[-1]
    list(connector.read_batches("orders", ["id"], cursor_column="id", after="10", max_rows=5))
    text, params, _ = [e for e in holder[0].executed if e[2] == "nexora_read"][0]
    assert text == 'SELECT "id" FROM "sales"."orders" WHERE "id" > %s ORDER BY "id" LIMIT %s'
    assert params == ["10", 6]


def test_read_batches_rejects_unknown_table_column_and_schema():
    connector, _ = make()
    with pytest.raises(TableNotFound):
        list(connector.read_batches("missing"))
    with pytest.raises(ColumnNotFound):
        list(connector.read_batches("orders", ["id", 'x"; drop table orders; --']))
    with pytest.raises(TableNotFound):
        list(connector.read_batches("orders", schema="public"))  # outside the configured schema


def test_read_batches_enforces_max_rows():
    connector, _ = make(catalog_responder(tables=TABLES, columns=COLUMNS, rows=[(i,) for i in range(4)]))
    with pytest.raises(ImportTooLarge):
        list(connector.read_batches("orders", ["id"], max_rows=3))


def test_empty_table_still_yields_the_header():
    connector, _ = make()
    (batch,) = list(connector.read_batches("orders"))
    assert batch.rows == [] and [c.name for c in batch.columns] == ["id", "total", "note"]


def test_reads_recheck_the_role_is_readonly():
    connector, _ = make(catalog_responder(tables=TABLES, columns=COLUMNS, role=(True, False, False, False, False)))
    with pytest.raises(RoleNotReadOnly) as e:
        list(connector.read_batches("orders"))
    assert "superuser" in e.value.extra["problems"]


def test_incremental_cursor():
    connector, _ = make(catalog_responder(tables=TABLES, columns=COLUMNS, max_value=42))
    state = connector.incremental_cursor("orders", "id")
    assert (state.column, state.value) == ("id", 42)
    with pytest.raises(ColumnNotFound):
        connector.incremental_cursor("orders", "nope")


# --- errors & secrets ---------------------------------------------------------------------------------------------
def test_auth_errors_are_mapped_and_password_never_logged(caplog):
    def refuse(**kwargs):
        raise psycopg.OperationalError(f'password authentication failed for user "x" (pw {SECRET})')

    connector = PostgresConnector(CONFIG, username="x", password=SECRET, connect_fn=refuse,
                                  resolver=public_resolver())
    with caplog.at_level(logging.DEBUG):
        result = connector.test()
    assert result.code == "connector_auth_failed" and SECRET not in result.message
    assert SECRET not in caplog.text


@pytest.mark.parametrize("message,code", [
    ("password authentication failed for user", "connector_auth_failed"),
    ("no pg_hba.conf entry for host", "connector_auth_failed"),
    ("SSL error: certificate verify failed", "connector_tls_failed"),
    ('database "nope" does not exist', "connector_database_not_found"),
    ("timeout expired", "connector_timeout"),
    ("connection refused", "connector_connection_failed"),
])
def test_map_error_codes(message, code):
    assert map_error(psycopg.OperationalError(message)).get_codes() == code


def test_auth_error_class():
    assert isinstance(map_error(psycopg.OperationalError("password authentication failed")), AuthenticationFailed)
