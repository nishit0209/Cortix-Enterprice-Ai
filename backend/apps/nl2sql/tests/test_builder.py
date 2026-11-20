"""Deterministic builder: 30+ question variants, every output parsed with sqlglot (postgres)."""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from apps.nl2sql.builder import build_sql
from apps.nl2sql.tests.conftest import TODAY, make_ctx

# (question, pattern, must-contain fragments)
CASES = [
    ("top 5 regions by revenue last month", "top_n",
     ['"region"', 'SUM("amount")', "LIMIT 5", "'2026-10-01'", "'2026-11-01'", "DESC"]),
    ("Top 10 product categories by units sold", "top_n", ['"product_category"', 'SUM("quantity")', "LIMIT 10"]),
    ("show me the top 3 channels by sales this year", "top_n", ['"channel"', "LIMIT 3", "'2026-01-01'", "'2027-01-01'"]),
    ("bottom 3 categories by units", "top_n", ['"product_category"', "ASC", "LIMIT 3"]),
    ("best 5 zones by average order value", "top_n", ['"region"', "COUNT(DISTINCT order_id)", "LIMIT 5"]),
    ("top regions by revenue", "top_n", ["LIMIT 10"]),
    ("lowest 2 regions by average discount in Q3 2026", "top_n",
     ['AVG("discount")', "ASC", "LIMIT 2", "'2026-07-01'", "'2026-10-01'"]),
    ("revenue by region", "by_dimension", ['GROUP BY "region"', "LIMIT 1000"]),
    ("Sales per channel last quarter", "by_dimension", ['GROUP BY "channel"', "'2026-07-01'", "'2026-10-01'"]),
    ("average discount by product category", "by_dimension", ['AVG("discount")', 'GROUP BY "product_category"']),
    ("number of customers by region", "by_dimension", ['COUNT(DISTINCT "customer_id")', 'GROUP BY "region"']),
    ("gross margin by territory in the last 30 days", "by_dimension",
     ["SUM(amount) - SUM(discount)", 'GROUP BY "region"', "'2026-10-19'", "'2026-11-18'"]),
    ("total revenue", "total", ['SUM("amount")']),
    ("what was total sales last month?", "total", ['SUM("amount")', "'2026-10-01'"]),
    ("how many orders last month", "total", ['COUNT(DISTINCT "order_id")', "'2026-10-01'"]),
    ("how much revenue did we make yesterday", "total", ["'2026-11-16'", "'2026-11-17'"]),
    ("average order value in 2025", "total", ["COUNT(DISTINCT order_id)", "'2025-01-01'", "'2026-01-01'"]),
    ("max amount this week", "total", ['MAX("amount")', "'2026-11-16'", "'2026-11-23'"]),
    ("median discount", "total", ['PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY "discount")']),
    ("revenue over time", "time_series", ["DATE_TRUNC('MONTH'", 'ORDER BY "month"']),
    ("monthly revenue in 2025", "time_series", ["DATE_TRUNC('MONTH'", "'2025-01-01'"]),
    ("weekly units sold", "time_series", ["DATE_TRUNC('WEEK'"]),
    ("revenue by month this year", "time_series", ["DATE_TRUNC('MONTH'", "'2026-01-01'"]),
    ("daily sales trend", "time_series", ["DATE_TRUNC('DAY'"]),
    ("revenue trend over the last 14 days", "time_series", ["DATE_TRUNC('DAY'", "'2026-11-04'"]),
    ("sales per quarter since 1 January 2025", "time_series", ["DATE_TRUNC('QUARTER'", "'2025-01-01'", "'2026-11-18'"]),
    ("compare revenue this month vs last month", "compare_periods",
     ["'current_period'", "'previous_period'", "'2026-11-01'", "'2026-10-01'", "'2026-12-01'"]),
    ("compare sales this week versus last week", "compare_periods", ["'current_period'", "'2026-11-09'", "'2026-11-23'"]),
    ("units last quarter vs this quarter", "compare_periods", ["'current_period'", "'2026-07-01'", "'2027-01-01'"]),
    ("compare gross margin this year vs last year", "compare_periods", ["SUM(amount) - SUM(discount)", "'2025-01-01'"]),
    ("revenue by region between 2026-01-01 and 2026-03-31", "by_dimension", ["'2026-01-01'", "'2026-04-01'"]),
    ("total sales in March 2026", "total", ["'2026-03-01'", "'2026-04-01'"]),
    ("revenue in january", "total", ["'2026-01-01'", "'2026-02-01'"]),
    ("total units ytd", "total", ["'2026-01-01'", "'2026-11-18'"]),
]


@pytest.mark.parametrize(("question", "pattern", "fragments"), CASES, ids=[c[0] for c in CASES])
def test_builder_variants(question, pattern, fragments, ctx):
    result = build_sql(question, ctx, today=TODAY)
    assert result is not None, f"builder declined: {question}"
    assert result.pattern == pattern
    ast = sqlglot.parse_one(result.sql, read="postgres")  # must parse
    assert isinstance(ast, exp.Select)
    assert [t.name for t in ast.find_all(exp.Table)] == ["sales"]  # logical table only
    for frag in fragments:
        assert frag in result.sql, f"{frag!r} not in {result.sql}"
    assert result.explanation and result.chart_hint is not None
    limit = ast.args.get("limit")
    if limit is not None:
        assert int(limit.expression.this) <= 1000


def test_at_least_twenty_variants():
    assert len(CASES) >= 20


@pytest.mark.parametrize("question", [
    "revenue by region in the west",               # filter value the builder cannot express
    "top 5 regions by revenue excluding returns",  # extra condition
    "revenue by region, channel and product",      # three dimensions
    "which customers churned",                     # not an aggregate question
    "top 5 regions by profitability",              # unknown measure
    "total widgets",                               # unknown measure
    "revenue in march vs revenue in may",          # two periods, not a this/last comparison
    "",
])
def test_builder_declines_instead_of_guessing(question, ctx):
    assert build_sql(question, ctx, today=TODAY) is None


def test_chart_hints(ctx):
    assert build_sql("top 5 regions by revenue", ctx, today=TODAY).chart_hint.type == "bar"
    assert build_sql("monthly revenue", ctx, today=TODAY).chart_hint.type == "line"
    assert build_sql("total revenue", ctx, today=TODAY).chart_hint.type == "kpi"
    hint = build_sql("revenue by region", ctx, today=TODAY).chart_hint
    assert hint.x == "region" and hint.y == ("total_amount",)


def test_fuzzy_typo_matching(ctx):
    r = build_sql("top 5 regoins by reveune", ctx, today=TODAY)
    assert r is not None and '"region"' in r.sql and 'SUM("amount")' in r.sql


def test_columns_not_allowed_are_never_used():
    ctx = make_ctx(allowed={"order_date", "region", "quantity"})  # amount withheld by the policy
    assert build_sql("total revenue", ctx, today=TODAY) is None
    assert build_sql("revenue by region", ctx, today=TODAY) is None
    r = build_sql("units by region", ctx, today=TODAY)
    assert r is not None and "amount" not in r.sql


def test_metric_referencing_withheld_column_is_unusable():
    ctx = make_ctx(allowed={"order_date", "region", "amount"})  # order_id withheld -> aov unusable
    assert build_sql("average order value by region", ctx, today=TODAY) is None


def test_time_filter_without_time_column_declines():
    cols = tuple(c for c in make_ctx().columns_meta["sales"] if c["name"] != "order_date")
    ctx = make_ctx(columns=cols)
    assert build_sql("revenue by region last month", ctx, today=TODAY) is None
    assert build_sql("revenue by region", ctx, today=TODAY) is not None


def test_role_inferred_from_type_when_missing():
    cols = ({"name": "sold_on", "type": "timestamp"}, {"name": "store", "type": "varchar"},
            {"name": "net_value", "type": "double precision"}, {"name": "store_id", "type": "int"})
    ctx = make_ctx(columns=cols)
    r = build_sql("net value by store last month", ctx, today=TODAY)
    assert r is not None
    assert '"sold_on" >=' in r.sql and 'SUM("net_value")' in r.sql


def test_multi_table_requires_columns_in_one_table():
    meta = {
        "sales": ({"name": "order_date", "type": "date"}, {"name": "amount", "type": "numeric"},
                  {"name": "store_id", "type": "int"}),
        "stores": ({"name": "store_id", "type": "int"}, {"name": "city", "type": "text"}),
    }
    ctx = make_ctx(tables={"sales": "s.a", "stores": "s.b"}, columns_meta=meta)
    assert build_sql("revenue by city", ctx, today=TODAY) is None  # needs a join -> Gemini
    r = build_sql("total amount", ctx, today=TODAY)
    assert r is not None and 'FROM "sales"' in r.sql


def test_identifiers_are_quoted_safely():
    cols = ({"name": 'we"ird', "type": "text", "role": "dimension"},
            {"name": "amount", "type": "numeric", "role": "measure"})
    ctx = make_ctx(columns=cols)
    r = build_sql('amount by we"ird', ctx, today=TODAY)
    if r is not None:
        sqlglot.parse_one(r.sql, read="postgres")
