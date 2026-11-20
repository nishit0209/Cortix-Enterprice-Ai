"""Warehouse on a plain local PostgreSQL 16/17 (pure, no DB): provisioning statements, PG16 CREATEROLE semantics
diagnostics, the actionable error for a reader role the loader cannot administer, and the spooled COPY reader."""

from __future__ import annotations

import polars as pl
import pytest
from psycopg import errors as pg_errors

from apps.warehouse import services
from apps.warehouse.exceptions import WarehouseSetupError

GOOD = {
    "server_version_num": 170002, "loader_createrole": True, "loader_superuser": False, "loader_owns_db": True,
    "executor_exists": True, "executor_inherit": False, "executor_superuser": False, "executor_bypassrls": False,
    "unadministrable_roles": [],
}


def _text(composed) -> str:
    """Render a psycopg sql.Composable without a connection (identifiers are always double-quoted)."""
    from psycopg import sql

    if isinstance(composed, sql.Composed):
        return "".join(_text(p) for p in composed._obj)
    if isinstance(composed, sql.Identifier):
        return ".".join('"' + s.replace('"', '""') + '"' for s in composed._obj)
    if isinstance(composed, sql.SQL):
        return composed._obj
    raise TypeError(type(composed))


def test_provisioning_statements_create_role_as_the_loader_then_grant_to_the_noinherit_executor():
    stmts = [_text(s) for s in services.provisioning_statements("org_abc", "wh_q_abc", "wh_executor")]
    assert stmts == [
        'CREATE SCHEMA IF NOT EXISTS "org_abc"',
        'REVOKE ALL ON SCHEMA "org_abc" FROM PUBLIC',
        'CREATE ROLE "wh_q_abc" NOLOGIN NOINHERIT',
        'GRANT USAGE ON SCHEMA "org_abc" TO "wh_q_abc"',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA "org_abc" GRANT SELECT ON TABLES TO "wh_q_abc"',
        'GRANT SELECT ON ALL TABLES IN SCHEMA "org_abc" TO "wh_q_abc"',
        'GRANT "wh_q_abc" TO "wh_executor"',
    ]
    # nothing needs superuser: no BYPASSRLS / SUPERUSER / CREATEROLE attributes are handed out, no WITH ADMIN
    joined = " ".join(stmts).upper()
    for forbidden in ("SUPERUSER", "BYPASSRLS", "CREATEROLE", "ADMIN OPTION", "INHERIT TRUE", "LOGIN "):
        assert forbidden not in joined.replace("NOLOGIN", "")


def test_setup_report_ok_for_bootstrap_roles():
    report = services.evaluate_setup(GOOD, "wh_executor")
    assert report.ok and report.problems == () and report.server_version == 170002


@pytest.mark.parametrize(("change", "fragment"), [
    ({"server_version_num": 150004}, "older than the supported 16+"),
    ({"loader_createrole": False}, "lacks CREATEROLE"),
    ({"loader_superuser": True}, "must not be a superuser"),
    ({"loader_owns_db": False}, "does not own the warehouse database"),
    ({"executor_exists": False}, "does not exist"),
    ({"executor_inherit": True}, "must be NOINHERIT"),
    ({"executor_bypassrls": True}, "must not be a superuser or BYPASSRLS"),
    ({"unadministrable_roles": ["wh_q_old1"]}, "DROP ROLE wh_q_old1"),
])
def test_setup_report_flags_each_problem(change, fragment):
    report = services.evaluate_setup({**GOOD, **change}, "wh_executor")
    assert not report.ok and any(fragment in p for p in report.problems), report.problems


def test_a_reader_role_the_loader_cannot_administer_gives_an_actionable_error(monkeypatch):
    def denied(*args):
        raise pg_errors.InsufficientPrivilege("permission denied to grant role \"wh_q_abc\"")

    monkeypatch.setattr(services, "_run_provisioning", denied)
    with pytest.raises(WarehouseSetupError) as exc:
        services._provision_objects("org_abc", "wh_q_abc", "wh_executor")
    assert exc.value.get_codes() == "warehouse_role_not_administrable"
    assert "GRANT wh_q_abc TO wh_loader WITH ADMIN OPTION" in str(exc.value.detail)


def test_frame_from_pg_csv_keeps_null_vs_empty_and_types(tmp_path):
    path = tmp_path / "copy.csv"
    # PostgreSQL COPY (FORMAT csv): NULL is an unquoted empty field, '' a quoted one
    path.write_bytes(b'1,"a",t,2024-01-02,2024-01-02 03:04:05+00,1.5\n,"",f,,,\n')
    cols = [services.ColumnSpec("id", "bigint"), services.ColumnSpec("name", "text"),
            services.ColumnSpec("ok", "boolean"), services.ColumnSpec("d", "date"),
            services.ColumnSpec("ts", "timestamptz"), services.ColumnSpec("x", "double precision")]
    frame = services.frame_from_pg_csv(str(path), [c.name for c in cols], cols)
    assert frame["id"].to_list() == [1, None]
    assert frame["name"].to_list() == ["a", ""]
    assert frame["ok"].to_list() == [True, False]
    assert frame["d"].dtype == pl.Date and frame["d"].null_count() == 1
    assert frame["ts"].dtype == pl.Datetime("us", "UTC") and frame["x"].to_list() == [1.5, None]


def test_frame_from_pg_csv_empty_result_keeps_the_schema(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    cols = [services.ColumnSpec("id", "bigint"), services.ColumnSpec("name", "text")]
    frame = services.frame_from_pg_csv(str(path), ["id", "name"], cols)
    assert frame.height == 0 and frame.schema == {"id": pl.Int64, "name": pl.String}


def test_copy_payload_is_cast_per_batch():
    df = pl.DataFrame({"id": [1, 2, 3], "name": ["a", None, "c\x00d"]})
    cols = [services.ColumnSpec("id", "bigint"), services.ColumnSpec("name", "text")]
    chunks = [services._frame_to_csv(services.prepare_frame(c, cols)) for c in df.iter_slices(n_rows=2)]
    assert chunks == [b'1,"a"\n2,\n', b'3,"cd"\n']
