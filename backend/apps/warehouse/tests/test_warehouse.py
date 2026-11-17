"""Warehouse provisioning, COPY loading, reads and the read-only executor against a real nexora_wh.

Runs as nexora_app (RLS on) for metadata and as wh_loader / wh_executor on nexora_wh. Objects use the
test-session prefix (settings.WH_SCHEMA_PREFIX) and are dropped at session end.
"""

import datetime as dt

import polars as pl
import pytest

from apps.accounts.models import User
from apps.core.tenancy import TenantContextMissing, tenant_context
from apps.orgs.services import create_organization
from apps.warehouse import naming, services
from apps.warehouse.db import loader_connection
from apps.warehouse.exceptions import (
    WarehouseError,
    WarehouseQueryDenied,
    WarehouseQueryTimeout,
    WarehouseTenantMismatch,
)
from apps.warehouse.models import WhOrgBinding
from apps.warehouse.services import ColumnSpec

pytestmark = pytest.mark.django_db

TABLE = "ds_aaaaaaaaaaaa_v1"
COLUMNS = [
    ColumnSpec("id", "bigint"),
    ColumnSpec("name", "text"),
    ColumnSpec("amount", "double precision"),
    ColumnSpec("price", "numeric"),
    ColumnSpec("flag", "boolean"),
    ColumnSpec("day", "date"),
    ColumnSpec("ts", "timestamptz"),
    ColumnSpec("meta", "jsonb"),
]


def sample_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "id": pl.Series([1, 2, None], dtype=pl.Int64),
        "name": ["alpha, \"quoted\"", "", None],
        "amount": [1.5, None, -3.25],
        "price": [10.0, 20.5, None],
        "flag": [True, False, None],
        "day": [dt.date(2024, 1, 5), None, dt.date(2023, 12, 31)],
        "ts": pl.Series([dt.datetime(2024, 1, 5, 10, 30), None, dt.datetime(2024, 2, 1, 0, 0, 0, 500000)]),
        "meta": ['{"a": 1}', None, "[1, 2]"],
    })


def wh_query(sql, params=None):
    with loader_connection(autocommit=True) as conn:
        return conn.execute(sql, params or []).fetchall()


def load_sample(org, table=TABLE):
    with tenant_context(org.id):
        return services.load_table(org.id, table, COLUMNS, sample_frame())


# --- provisioning -------------------------------------------------------------------------------------
def test_provision_creates_schema_nologin_role_and_grants_idempotently(two_orgs):
    _, org_a, _, _ = two_orgs
    with tenant_context(org_a.id):
        first = services.provision(org_a.id)
        second = services.provision(org_a.id)
        assert first.id == second.id
        assert WhOrgBinding.objects.count() == 1
        assert services.reader_role(org_a.id) == naming.reader_role_name(org_a.short_id)
    schema, role = naming.schema_name(org_a.short_id), naming.reader_role_name(org_a.short_id)
    assert first.wh_schema == schema and first.reader_role == role
    assert wh_query("SELECT count(*) FROM pg_namespace WHERE nspname = %s", [schema])[0][0] == 1
    assert wh_query("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", [role])[0][0] is False
    assert wh_query("SELECT has_schema_privilege(%s, %s, 'USAGE')", [role, schema])[0][0] is True
    assert wh_query("SELECT has_schema_privilege(%s, %s, 'CREATE')", [role, schema])[0][0] is False
    assert wh_query("SELECT pg_has_role(%s, %s, 'MEMBER')", [naming.executor_login_role(), role])[0][0] is True


def test_binding_is_tenant_scoped(two_orgs):
    _, org_a, _, org_b = two_orgs
    with tenant_context(org_a.id):
        services.provision(org_a.id)
    with tenant_context(org_b.id):
        assert WhOrgBinding.objects.count() == 0
        with pytest.raises(WarehouseTenantMismatch):
            services.provision(org_a.id)


def test_org_created_signal_provisions_the_warehouse(django_capture_on_commit_callbacks):
    owner = User.objects.create_user("signal-owner@example.test", "pw-Owner-123456")
    with django_capture_on_commit_callbacks(execute=True):
        org = create_organization(name="Signal Org", owner=owner)
    with tenant_context(org.id):
        binding = WhOrgBinding.objects.get()
    assert binding.wh_schema == naming.schema_name(org.short_id)


# --- loading & reading --------------------------------------------------------------------------------
def test_load_creates_immutable_typed_table_and_reads_back(two_orgs):
    _, org_a, _, _ = two_orgs
    result = load_sample(org_a)
    assert result.row_count == 3 and result.byte_size > 0
    schema = naming.schema_name(org_a.short_id)
    types = dict(wh_query(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
        [schema, TABLE],
    ))
    assert types == {
        "id": "bigint", "name": "text", "amount": "double precision", "price": "numeric", "flag": "boolean",
        "day": "date", "ts": "timestamp with time zone", "meta": "jsonb",
    }
    with tenant_context(org_a.id):
        back = services.read_table(org_a.id, TABLE, COLUMNS)
        with pytest.raises(WarehouseError) as exc:
            services.load_table(org_a.id, TABLE, COLUMNS, sample_frame())
    assert exc.value.get_codes() == "warehouse_table_exists"
    assert back["id"].to_list() == [1, 2, None]
    assert back["name"].to_list() == ['alpha, "quoted"', "", None]  # '' and NULL stay distinct
    assert back["flag"].to_list() == [True, False, None]
    assert back["day"].to_list() == [dt.date(2024, 1, 5), None, dt.date(2023, 12, 31)]
    assert back["ts"][0] == dt.datetime(2024, 1, 5, 10, 30, tzinfo=dt.UTC)
    assert back["ts"][2] == dt.datetime(2024, 2, 1, 0, 0, 0, 500000, tzinfo=dt.UTC)
    assert back["meta"][0] == '{"a": 1}'
    assert back["price"].to_list() == [10.0, 20.5, None]


def test_read_table_limit_and_drop_table(two_orgs):
    _, org_a, _, _ = two_orgs
    load_sample(org_a)
    with tenant_context(org_a.id):
        assert services.read_table(org_a.id, TABLE, COLUMNS[:1], limit=2).height == 2
        services.drop_table(org_a.id, TABLE)
    assert wh_query("SELECT to_regclass(%s)", [f"{naming.schema_name(org_a.short_id)}.{TABLE}"])[0][0] is None


def test_physical_table(two_orgs):
    _, org_a, _, org_b = two_orgs

    class V:
        org_id = org_a.id
        wh_schema = naming.schema_name(org_a.short_id)
        wh_table = TABLE

    with tenant_context(org_a.id):
        assert services.physical_table(V) == f"{V.wh_schema}.{TABLE}"
    with tenant_context(org_b.id), pytest.raises(WarehouseTenantMismatch):
        services.physical_table(V)


# --- read-only executor -----------------------------------------------------------------------------
def test_executor_reads_with_search_path_and_caps(two_orgs):
    _, org_a, _, _ = two_orgs
    load_sample(org_a)
    with tenant_context(org_a.id):
        rs = services.execute_readonly_sql(org_a.id, f"SELECT id, name FROM {TABLE} ORDER BY id NULLS LAST")
        assert [c.name for c in rs.columns] == ["id", "name"]
        assert [c.type for c in rs.columns] == ["bigint", "text"]
        assert rs.rows == ((1, 'alpha, "quoted"'), (2, ""), (None, None)) and not rs.truncated
        capped = services.execute_readonly_sql(org_a.id, f"SELECT * FROM {TABLE}", row_cap=2)
        assert capped.row_count == 2 and capped.truncated
        exact = services.execute_readonly_sql(org_a.id, f"SELECT * FROM {TABLE}", row_cap=3)
        assert exact.row_count == 3 and not exact.truncated
        tiny = services.execute_readonly_sql(org_a.id, f"SELECT * FROM {TABLE}", byte_cap=10)
        assert tiny.row_count == 0 and tiny.truncated
        params = services.execute_readonly_sql(org_a.id, f"SELECT id FROM {TABLE} WHERE id = %s", [2])
        assert params.rows == ((2,),)
        assert services.execute_readonly_sql(org_a.id, "SELECT current_user").rows[0][0] == naming.reader_role_name(
            org_a.short_id
        )


def test_executor_refuses_other_org_and_missing_context(two_orgs):
    _, org_a, _, org_b = two_orgs
    with tenant_context(org_a.id), pytest.raises(WarehouseTenantMismatch):
        services.execute_readonly_sql(org_b.id, "SELECT 1")
    with pytest.raises(TenantContextMissing):
        services.execute_readonly_sql(org_a.id, "SELECT 1")


@pytest.mark.ac("P3-AC6")
def test_reader_role_cannot_read_another_orgs_schema(two_orgs):
    _, org_a, _, org_b = two_orgs
    load_sample(org_b)
    with tenant_context(org_a.id):
        services.provision(org_a.id)
        with pytest.raises(WarehouseQueryDenied):
            services.execute_readonly_sql(org_a.id, f"SELECT * FROM {naming.schema_name(org_b.short_id)}.{TABLE}")
        # unqualified names resolve only inside the caller's own schema
        with pytest.raises(WarehouseError):
            services.execute_readonly_sql(org_a.id, f"SELECT * FROM {TABLE}")


@pytest.mark.parametrize(
    "statement",
    [
        f"CREATE TABLE {TABLE}_x (a int)",
        f"WITH d AS (DELETE FROM {TABLE} RETURNING *) SELECT * FROM d",
        "SELECT set_config('role', 'wh_executor', true)",
        "SELECT 1; SELECT 2",
        "",
    ],
)
def test_executor_refuses_writes_and_session_changes(two_orgs, statement):
    _, org_a, _, _ = two_orgs
    load_sample(org_a)
    with tenant_context(org_a.id), pytest.raises(WarehouseError):
        services.execute_readonly_sql(org_a.id, statement)
    assert wh_query(f"SELECT count(*) FROM {naming.schema_name(org_a.short_id)}.{TABLE}")[0][0] == 3


def test_executor_statement_timeout(two_orgs):
    _, org_a, _, _ = two_orgs
    with tenant_context(org_a.id):
        services.provision(org_a.id)
        with pytest.raises(WarehouseQueryTimeout):
            services.execute_readonly_sql(org_a.id, "SELECT pg_sleep(3)", timeout_s=0.3)


def test_preview_rows(two_orgs):
    _, org_a, _, _ = two_orgs
    load_sample(org_a)
    with tenant_context(org_a.id):
        rs = services.preview_rows(org_a.id, TABLE, ["id", "day"], limit=2)
    assert rs.row_count == 2 and [c.name for c in rs.columns] == ["id", "day"]
