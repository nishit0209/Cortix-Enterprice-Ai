"""W3 connectors (pure, no DB): connection-test checks for the UI, qualified table names from the catalog browser,
catalog aliases and the data-stats provider shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.connectors import services
from apps.connectors.base import ColumnInfo, ReadonlyCheck, TableInfo, TestResult


# ------------------------------------------------------------------------------------------------ test checks
def test_a_passing_test_lists_every_check_in_order():
    result = TestResult(ok=True, server_version="17.2", current_user="nexora_ro", database="shop",
                        resolved_ip="52.10.20.30", readonly=ReadonlyCheck(ok=True), latency_ms=7)
    checks = result.as_dict()["checks"]
    assert [c["key"] for c in checks] == ["dns", "ssrf", "tls", "login", "readonly"]
    assert all(c["ok"] for c in checks)
    assert "52.10.20.30" in checks[0]["message"] and "7 ms" in checks[2]["message"]
    assert "nexora_ro" in checks[3]["message"] and "PostgreSQL 17.2" in checks[3]["message"]


@pytest.mark.parametrize(("code", "logged_in", "failed_key"), [
    ("connector_host_invalid", False, "dns"),
    ("connector_host_unresolvable", False, "dns"),
    ("connector_host_forbidden", False, "ssrf"),
    ("connector_connection_failed", False, "tls"),
    ("connector_timeout", False, "tls"),
    ("connector_tls_failed", False, "tls"),
    ("connector_auth_failed", False, "login"),
    ("connector_role_not_readonly", True, "readonly"),
    ("connector_query_failed", True, "readonly"),
    ("connector_error", False, "tls"),
])
def test_checks_stop_at_the_failing_step(code, logged_in, failed_key):
    result = TestResult(ok=False, code=code, message="It failed.", current_user="u" if logged_in else "")
    checks = result.checks()
    assert checks[-1] == {"key": failed_key, "ok": False, "message": "It failed."}
    assert all(c["ok"] for c in checks[:-1])
    keys = [c["key"] for c in checks]
    assert keys == ["dns", "ssrf", "tls", "login", "readonly"][: len(keys)]


def test_readonly_failure_names_the_problems():
    problems = tuple(f"write:sales.t{i}" for i in range(7))
    result = TestResult(ok=False, code="connector_role_not_readonly", current_user="writer", message="Not read-only.",
                        readonly=ReadonlyCheck(ok=False, problems=problems))
    last = result.checks()[-1]
    assert last["key"] == "readonly" and not last["ok"]
    assert "write:sales.t0" in last["message"] and "write:sales.t4" in last["message"]
    assert "write:sales.t5" not in last["message"] and last["message"].endswith("…")


def test_checks_never_contain_the_secret():
    result = TestResult(ok=False, code="connector_auth_failed", message="password authentication failed")
    assert "Synthetic" not in repr(result.as_dict())


# ------------------------------------------------------------------------------------------------ qualified tables
def _source(catalog=None, schema="sales"):
    return SimpleNamespace(catalog=catalog, config={"schema": schema} if schema else {})


CATALOG = {"schemas": [{"name": "sales", "tables": [{"name": "orders"}, {"name": "fy.2024"}]}]}


@pytest.mark.parametrize(("source", "table", "expected"), [
    (_source(CATALOG), "sales.orders", ("sales", "orders")),        # what the catalog browser sends
    (_source(CATALOG), "orders", (None, "orders")),                 # bare name: unchanged
    (_source(CATALOG), "sales.fy.2024", ("sales", "fy.2024")),      # dotted table name listed in the catalog
    (_source(CATALOG), "hr.people", (None, "hr.people")),           # not in the catalog: kept as one name
    (_source(None), "sales.orders", ("sales", "orders")),           # no catalog yet: configured schema prefix
    (_source(None), "fy.2024", (None, "fy.2024")),                  # no catalog, prefix is not the schema
    (_source(None, schema=None), "public.orders", (None, "public.orders")),
    (_source(CATALOG), ".orders", (None, ".orders")),
    (_source(CATALOG), "sales.", (None, "sales.")),
])
def test_split_qualified_table(source, table, expected):
    assert services.split_qualified_table(source, table) == expected


def test_import_serializer_accepts_a_qualified_name_up_to_two_identifiers():
    from apps.connectors.serializers import TABLE_MAX, ImportSerializer

    long_name = "s" * TABLE_MAX + "." + "t" * TABLE_MAX
    assert ImportSerializer(data={"table": long_name, "name": "x"}).is_valid()
    assert not ImportSerializer(data={"table": long_name + "x", "name": "x"}).is_valid()


# ------------------------------------------------------------------------------------------------ catalog
def test_catalog_payload_carries_the_row_estimate_alias():
    tables = [TableInfo(schema="sales", name="orders", estimated_rows=30,
                        columns=(ColumnInfo("id", "bigint", False, 1),))]
    payload = services.catalog_payload(tables)
    (table,) = payload["schemas"][0]["tables"]
    assert table["estimated_rows"] == table["row_estimate"] == 30
    assert table["columns"] == [{"name": "id", "type": "bigint", "nullable": False}]
