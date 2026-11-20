"""sqlguard.execute against a real nexora_wh: row policies, masks, caps, EXPLAIN checks, timeouts, cross-org.

Tables are loaded with the warehouse loader into the org's test-prefixed schema; queries run as wh_executor ->
SET LOCAL ROLE <org reader>. All data is synthetic.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import polars as pl
import pytest

from apps.core.tenancy import tenant_context
from apps.sqlguard import services
from apps.sqlguard.exceptions import GuardError
from apps.warehouse import services as warehouse
from apps.warehouse.services import ColumnSpec
from nexora.contracts import MaskSpec, QueryPolicy, QueryTarget, RowPredicate

pytestmark = pytest.mark.django_db

TABLE = "ds_cccccccccccc_v1"
COLUMNS = [ColumnSpec("order_id", "bigint"), ColumnSpec("region", "text"), ColumnSpec("revenue", "numeric"),
           ColumnSpec("phone", "text"), ColumnSpec("secret", "text")]


def load(org, rows: int = 6, table: str = TABLE):
    frame = pl.DataFrame({
        "order_id": list(range(1, rows + 1)),
        "region": [["North", "South", "East"][i % 3] for i in range(rows)],
        "revenue": [float(10 * (i + 1)) for i in range(rows)],
        "phone": [f"98000000{i:02d}" for i in range(rows)],
        "secret": [f"s-{i}" for i in range(rows)],
    })
    with tenant_context(org.id):
        return warehouse.load_table(org.id, table, COLUMNS, frame)


def policy_for(org, loaded, **kw) -> QueryPolicy:
    base = QueryPolicy(
        org_id=org.id, user_id=uuid.uuid4(), dataset_id=uuid.uuid4(),
        tables={"sales": loaded.physical},
        allowed_columns={"sales": frozenset({"order_id", "region", "revenue", "phone"})},
        row_cap=100, timeout_s=10, fingerprint="db-test",
    )
    return replace(base, **kw)


def run(org, sql, policy, target_org=None):
    result = services.validate(sql, policy)
    assert result.ok, (result.reject_code, result.message)
    with tenant_context(org.id):
        return services.execute(result, policy, QueryTarget(kind="warehouse", org_id=(target_org or org).id))


def test_executes_and_returns_typed_rows(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    rs = run(org_a, "SELECT region, sum(revenue) AS total FROM sales GROUP BY region ORDER BY region",
             policy_for(org_a, loaded))
    assert [c.name for c in rs.columns] == ["region", "total"]
    assert [(r[0], float(r[1])) for r in rs.rows] == [("East", 90.0), ("North", 50.0), ("South", 70.0)]
    assert not rs.truncated and rs.plan_cost is not None and rs.plan_cost > 0


def test_row_policy_filters_rows_even_with_or_true(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded, row_predicates={"sales": RowPredicate((("region", "=", "North"),))})
    rs = run(org_a, "SELECT region, order_id FROM sales WHERE region = 'South' OR 1=1", policy)
    assert {r[0] for r in rs.rows} == {"North"} and rs.row_count == 2


def test_deny_all_row_policy_returns_nothing(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded, row_predicates={"sales": RowPredicate((("region", "in", ()),))})
    assert run(org_a, "SELECT count(*) AS n FROM sales", policy).rows == ((0,),)


def test_masks_are_applied_at_the_source(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded, column_masks={("sales", "phone"): MaskSpec("mask_partial", keep_last=2),
                                                      ("sales", "revenue"): MaskSpec("mask_null")})
    rs = run(org_a, "SELECT phone, revenue FROM sales ORDER BY order_id", policy)
    assert rs.rows[0] == ("********00", None)
    assert [c.masked for c in rs.columns] == [True, True]
    # filtering on the masked value never matches the raw phone number
    assert run(org_a, "SELECT count(*) AS n FROM sales WHERE phone = '9800000000'", policy).rows == ((0,),)


def test_mask_hash_is_stable_and_hides_the_value(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded, column_masks={("sales", "phone"): MaskSpec("mask_hash")})
    rows = run(org_a, "SELECT phone FROM sales ORDER BY order_id", policy).rows
    # SECURITY_ARCHITECTURE §5: hex sha256 of the value + org salt (64 hex chars)
    assert len(rows[0][0]) == 64 and rows[0][0] != "9800000000"
    assert all(ch in "0123456789abcdef" for ch in rows[0][0])
    assert rows == run(org_a, "SELECT phone FROM sales ORDER BY order_id", policy).rows


def test_denied_column_cannot_be_read(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded)
    assert services.validate("SELECT secret FROM sales", policy).reject_code == "column_not_allowed"
    rs = run(org_a, "SELECT * FROM sales", policy)
    assert "secret" not in [c.name for c in rs.columns]


def test_row_cap_truncates(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    rs = run(org_a, "SELECT order_id FROM sales", policy_for(org_a, loaded, row_cap=2))
    assert rs.row_count == 2 and rs.truncated


def test_byte_cap_truncates(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    rs = run(org_a, "SELECT phone FROM sales", policy_for(org_a, loaded, byte_cap=30))
    assert rs.truncated and rs.row_count < 6


def test_cross_org_execution_is_refused(two_orgs):
    _, org_a, _, org_b = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded)
    result = services.validate("SELECT region FROM sales", policy)
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        services.execute(result, policy, QueryTarget(kind="warehouse", org_id=org_b.id))
    assert exc.value.reject_code == "execution_error"
    with tenant_context(org_b.id), pytest.raises(GuardError):
        services.execute(result, policy, QueryTarget(kind="warehouse", org_id=org_a.id))


@pytest.mark.ac("P3-AC6")
def test_policy_pointing_at_another_orgs_table_fails_in_the_database(two_orgs):
    _, org_a, _, org_b = two_orgs
    load(org_a)
    loaded_b = load(org_b)
    forged = policy_for(org_a, loaded_b)  # org A's policy naming org B's physical table
    result = services.validate("SELECT region FROM sales", forged)
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        services.execute(result, forged, QueryTarget(kind="warehouse", org_id=org_a.id))
    assert exc.value.reject_code == "execution_error"  # permission denied for org B's schema


def test_explain_refuses_relations_outside_the_policy(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    other = load(org_a, table="ds_dddddddddddd_v1")
    policy = policy_for(org_a, loaded)
    narrow = policy_for(org_a, other)
    result = services.validate("SELECT region FROM sales", policy)
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        warehouse.execute_readonly_sql(org_a.id, result.executed_sql, dict(result.params), search_path="pg_catalog",
                                       plan_check=services.make_plan_check(narrow))
    assert exc.value.reject_code == "plan_relation_not_allowed"


def test_cost_cap(two_orgs, settings):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    settings.NL2SQL_MAX_COST = 0.01
    result = services.validate("SELECT count(*) FROM sales a, sales b", policy_for(org_a, loaded))
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        services.execute(result, policy_for(org_a, loaded), QueryTarget(kind="warehouse", org_id=org_a.id))
    assert exc.value.reject_code == "cost_exceeded"


def test_statement_timeout(two_orgs, settings):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a, rows=3000)
    settings.NL2SQL_MAX_COST = 1e18
    policy = policy_for(org_a, loaded, timeout_s=1)
    result = services.validate("SELECT count(*) FROM sales a, sales b, sales c WHERE a.revenue + b.revenue > c.revenue",
                               policy)
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        services.execute(result, policy, QueryTarget(kind="warehouse", org_id=org_a.id))
    assert exc.value.reject_code == "timeout" and exc.value.status_code == 504


def test_string_parameters_in_select_and_group_by(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    rs = run(org_a, "SELECT CASE WHEN region = 'North' THEN 'n' ELSE 'other' END AS k, count(*) AS c "
                    "FROM sales WHERE region LIKE '%th' GROUP BY 1 ORDER BY 1", policy_for(org_a, loaded))
    assert rs.rows == (("n", 2), ("other", 2))


def test_forged_guard_result_is_refused_before_the_database(two_orgs):
    _, org_a, _, _ = two_orgs
    loaded = load(org_a)
    policy = policy_for(org_a, loaded)
    good = services.validate("SELECT region FROM sales", policy)
    forged = replace(good, executed_sql=f"SELECT secret FROM {loaded.physical}")
    with tenant_context(org_a.id), pytest.raises(GuardError) as exc:
        services.execute(forged, policy, QueryTarget(kind="warehouse", org_id=org_a.id))
    assert exc.value.status_code == 403
