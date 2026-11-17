"""Grammar-based Hypothesis fuzzing of the SQL guard (TESTING_STRATEGY §4.2).

Property 1: a query with ANY injected forbidden construct is never accepted.
Property 2: clean queries never crash the guard, and every accepted query satisfies the oracle invariants.

Example counts: NEXORA_FUZZ_EXAMPLES (default 300 per push; nightly sets 10000).
The strategies themselves are exercised without the guard (test_strategies_generate_parseable_sql) so the
scaffolding is verified before apps.sqlguard ships.
"""

from __future__ import annotations

import os

import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from testing.sqlguard import build_policy, get_validate, oracle_violations, policy_spec

VALIDATE = get_validate()
EXAMPLES = int(os.environ.get("NEXORA_FUZZ_EXAMPLES", "300"))
FUZZ_SETTINGS = settings(
    max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow], derandomize=False
)

SPEC = policy_spec("default")
SALES_COLS = sorted(SPEC["allowed_columns"]["sales"])
NUMERIC = ["quantity", "unit_price", "discount", "revenue"]
DIMENSIONS = ["region", "product", "category", "channel"]
SAFE_FUNCS = ["sum", "avg", "min", "max", "count"]

# forbidden constructs by insertion slot ---------------------------------------------------------------
FORBIDDEN_SELECT_ITEMS = [
    "pg_sleep(1)", "current_setting('server_version')", "version()", "pg_read_file('x')", "txid_current()",
    "email", "phone", "pan_number", "cost_price", "margin_secret", "ctid", "xmin", "row_to_json(sales)",
    "(SELECT rolname FROM pg_roles LIMIT 1)", "(SELECT count(*) FROM org_b.ds_1)", "'pg_class'::regclass",
    "repeat('x', 100)", "query_to_xml('select 1', true, true, '')", "inet_server_addr()",
]
FORBIDDEN_FROM = [
    "org_b.ds_1", "pg_catalog.pg_class", "information_schema.tables", "pg_stat_activity", "nexora_wh.org_b.t",
    "generate_series(1, 10)", "public.accounts_user", "pg_shadow",
]
FORBIDDEN_WHERE = [
    "email LIKE 'a%'", "EXISTS (SELECT 1 FROM pg_roles)", "pg_sleep(1) IS NULL", "phone = '1'",
    "order_id IN (SELECT order_id FROM org_b.ds_1)", "cost_price > 0", "set_config('x', 'y', true) = 'y'",
]
FORBIDDEN_SUFFIX = ["; DROP TABLE sales", " FOR UPDATE", "; SELECT 1", " FOR SHARE"]


@st.composite
def clean_select(draw):
    dims = draw(st.lists(st.sampled_from(DIMENSIONS), min_size=0, max_size=2, unique=True))
    aggs = draw(st.lists(st.tuples(st.sampled_from(SAFE_FUNCS), st.sampled_from(NUMERIC)), min_size=0, max_size=2))
    plain = draw(st.lists(st.sampled_from(SALES_COLS), min_size=1, max_size=3, unique=True)) if not (dims or aggs) else []
    items = dims + [f"{f}({c}) AS {f}_{c}" for f, c in aggs] + plain
    sql = f"SELECT {', '.join(items)} FROM sales"
    if draw(st.booleans()):
        col = draw(st.sampled_from(NUMERIC))
        sql += f" WHERE {col} > {draw(st.integers(min_value=0, max_value=1000))}"
    if dims and aggs:
        sql += " GROUP BY " + ", ".join(dims)
    if draw(st.booleans()):
        sql += f" LIMIT {draw(st.integers(min_value=1, max_value=500))}"
    return sql


@st.composite
def injected_select(draw):
    base = draw(clean_select())
    slot = draw(st.sampled_from(["select", "from", "where", "suffix"]))
    if slot == "select":
        return base.replace("SELECT ", f"SELECT {draw(st.sampled_from(FORBIDDEN_SELECT_ITEMS))}, ", 1)
    if slot == "from":
        return base.replace(" FROM sales", f" FROM sales, {draw(st.sampled_from(FORBIDDEN_FROM))} AS x", 1)
    if slot == "where":
        cond = draw(st.sampled_from(FORBIDDEN_WHERE))
        if " WHERE " in base:
            return base.replace(" WHERE ", f" WHERE {cond} AND ", 1)
        return base.replace(" FROM sales", f" FROM sales WHERE {cond}", 1)
    tail = draw(st.sampled_from(FORBIDDEN_SUFFIX))
    return base + tail


@FUZZ_SETTINGS
@given(clean_select())
def test_strategies_generate_parseable_sql(sql):
    assert len(sqlglot.parse(sql, read="postgres")) == 1


@pytest.mark.skipif(VALIDATE is None, reason="apps.sqlguard.services.validate not implemented yet (wave 2)")
@pytest.mark.ac("P4-AC1")
@FUZZ_SETTINGS
@given(injected_select())
def test_injected_forbidden_constructs_are_never_accepted(sql):
    result = VALIDATE(sql, build_policy("default"))
    assert result.ok is False, f"accepted forbidden query: {sql!r} -> {result.executed_sql!r}"


@pytest.mark.skipif(VALIDATE is None, reason="apps.sqlguard.services.validate not implemented yet (wave 2)")
@FUZZ_SETTINGS
@given(clean_select(), st.sampled_from(["default", "north_only"]))
def test_clean_queries_never_crash_and_accepted_ones_are_safe(sql, policy_name):
    result = VALIDATE(sql, build_policy(policy_name))
    if result.ok:
        assert oracle_violations(result, policy_name) == []
    else:
        assert result.reject_code, sql
