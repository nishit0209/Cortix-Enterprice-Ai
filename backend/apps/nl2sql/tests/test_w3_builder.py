"""W3 deterministic builder: common phrasings, verified by RESULT (not SQL text) on the synthetic sales CSV.

Every SQL the builder produces here is executed on QA's in-memory SQLite copy of testdata/generated/sales.csv
(testing.golden.run_sql) and compared with an answer computed independently in Python (testing.golden
expected_result / plain Python for growth, share and ratio shapes). No database, no Gemini.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

import pytest

from apps.nl2sql.builder import build_sql
from apps.nl2sql.values import apply_values
from nexora.contracts import MaskSpec, RowPredicate
from testing import golden

TODAY = golden.today()  # 2026-11-17, the generator anchor


@pytest.fixture(scope="module")
def ctx():
    return golden.question_context()


def profile_top_values(rows, columns, n=10):
    """What the datasets profiler stores: the n most frequent values per column (count desc, value asc)."""
    out = {}
    for col in columns:
        counts = Counter(r[col] for r in rows if r[col] not in (None, ""))
        out[col] = tuple(v for v, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n])
    return out


@pytest.fixture(scope="module")
def ctx_values(ctx):
    return apply_values(ctx, profile_top_values(golden.sales_rows(), ("region", "product", "category", "channel")))


def run(question, c):
    built = build_sql(question, c, today=TODAY)
    assert built is not None, f"builder declined {question!r}"
    columns, rows = golden.run_sql(built.sql)
    return built, columns, rows


def assert_answers(question, c, check):
    built, columns, rows = run(question, c)
    problems = golden.compare(golden.expected_result(check), columns, rows)
    assert not problems, (question, built.sql, problems)
    return built


THIS_YEAR = {"relative": "this", "unit": "year"}
LAST_YEAR = {"relative": "last", "unit": "year"}

PHRASINGS = [
    ("revenue this year", {"type": "total", "agg": "sum", "measure": "revenue", "period": THIS_YEAR}),
    ("revenue last year", {"type": "total", "agg": "sum", "measure": "revenue", "period": LAST_YEAR}),
    ("number of customers last year",
     {"type": "total", "agg": "count_distinct", "measure": "customer_id", "period": LAST_YEAR}),
    ("how many orders did we get this year",
     {"type": "total", "agg": "count_distinct", "measure": "order_id", "period": THIS_YEAR}),
    ("top 3 products by revenue this year",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "product", "top": 3, "order": "desc",
      "period": THIS_YEAR}),
    ("which region had the highest revenue last year",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "region", "top": 1, "order": "desc",
      "period": LAST_YEAR}),
    ("which channel has the lowest revenue",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "channel", "top": 1, "order": "asc"}),
    ("top 3 categories by number of orders",
     {"type": "group", "agg": "count_distinct", "measure": "order_id", "by": "category", "top": 3, "order": "desc"}),
    ("top 5 regions by average discount",
     {"type": "group", "agg": "avg", "measure": "discount", "by": "region", "top": 5, "order": "desc"}),
    ("sales by category last quarter",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "category",
      "period": {"relative": "last", "unit": "quarter"}}),
    ("revenue by region this year",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "region", "period": THIS_YEAR}),
    ("revenue by quarter in 2025",
     {"type": "series", "agg": "sum", "measure": "revenue", "grain": "quarter",
      "period": {"start": "2025-01-01", "end": "2026-01-01"}}),
    ("orders per month this year",
     {"type": "series", "agg": "count_distinct", "measure": "order_id", "grain": "month", "period": THIS_YEAR}),
    ("average discount by month", {"type": "series", "agg": "avg", "measure": "discount", "grain": "month"}),
    ("yearly revenue", {"type": "series", "agg": "sum", "measure": "revenue", "grain": "year"}),
    ("weekly orders last month",  # g39: a bare entity noun counts it inside a time series
     {"type": "series", "agg": "count_distinct", "measure": "order_id", "grain": "week",
      "period": {"relative": "last", "unit": "month"}}),
    # named dimension values: no value list needed ("<prep> [the] <value> <dimension>")
    ("revenue in the north region", {"type": "total", "agg": "sum", "measure": "revenue", "where": {"region": "North"}}),
    ("monthly revenue for the online channel",
     {"type": "series", "agg": "sum", "measure": "revenue", "grain": "month", "where": {"channel": "Online"}}),
]


@pytest.mark.parametrize(("question", "check"), PHRASINGS, ids=[q for q, _ in PHRASINGS])
def test_common_phrasings_answer_correctly(ctx, question, check):
    assert_answers(question, ctx, check)


VALUE_PHRASINGS = [
    ("total revenue from electronics",
     {"type": "total", "agg": "sum", "measure": "revenue", "where": {"category": "Electronics"}}),
    ("revenue for electroncs",  # one-edit typo of a known value
     {"type": "total", "agg": "sum", "measure": "revenue", "where": {"category": "Electronics"}}),
    ("online orders last month",
     {"type": "total", "agg": "count_distinct", "measure": "order_id", "where": {"channel": "Online"},
      "period": {"relative": "last", "unit": "month"}}),
    ("top 3 products by revenue in the west",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "product", "top": 3, "order": "desc",
      "where": {"region": "West"}}),
    ("revenue by category for the north region",
     {"type": "group", "agg": "sum", "measure": "revenue", "by": "category", "where": {"region": "North"}}),
]


@pytest.mark.parametrize(("question", "check"), VALUE_PHRASINGS, ids=[q for q, _ in VALUE_PHRASINGS])
def test_known_value_filters_answer_correctly(ctx, ctx_values, question, check):
    built = assert_answers(question, ctx_values, check)
    col, value = next(iter(check["where"].items()))
    assert f"\"{col}\" = '{value}'" in built.sql  # the stored casing, an exact match (index friendly)
    assert any(value in a for a in built.assumptions)


def test_without_value_lists_the_unnamed_values_are_declined(ctx):
    for question in ("total revenue from electronics", "online orders last month",
                     "top 3 products by revenue in the west"):
        assert build_sql(question, ctx, today=TODAY) is None, question


def test_golden_set_builder_accuracy_with_profiled_values(ctx_values):
    """QA's 48 golden questions: the 3 value-filter questions (g46-g48) need the profiled values that production
    passes in (apps.nl2sql.values); with them the builder answers all 48 correctly."""
    outcomes = []
    for case in golden.golden()["questions"]:
        built = build_sql(case["question"], ctx_values, today=TODAY)
        if built is None:
            outcomes.append((case["id"], "declined"))
            continue
        columns, rows = golden.run_sql(built.sql)
        problems = golden.compare(golden.expected_result(case["check"]), columns, rows)
        outcomes.append((case["id"], problems[0] if problems else "ok"))
    assert [o for o in outcomes if o[1] != "ok"] == []
    assert len(outcomes) == 48


def test_golden_set_builder_accuracy_on_qa_context(ctx):
    """Builder-only on QA's context exactly as the golden runner uses it (no value lists): 45/48, all correct."""
    report = golden.report(use_cassettes=False)
    assert report["builder_correct"] == 45 and report["precision_when_answered"] == 1.0
    declined = sorted(o.id for o in report["outcomes"] if o.source == "none")
    assert declined == ["g46", "g47", "g48"]


# ------------------------------------------------------------------------------------------------ shapes
def _by_month(rows):
    out = defaultdict(float)
    for r in rows:
        out[r["order_date"][:7]] += r["revenue"] or 0.0
    return dict(sorted(out.items()))


def test_share_of_total(ctx):
    for question in ("share of revenue by region", "what percentage of revenue comes from each region",
                     "revenue share by region"):
        built, columns, rows = run(question, ctx)
        assert columns == ["region", "total_revenue", "share_pct"], question
        totals = defaultdict(float)
        for r in golden.sales_rows():
            totals[r["region"]] += r["revenue"] or 0.0
        grand = sum(totals.values())
        for region, total, pct in rows:
            assert total == pytest.approx(totals[region], abs=0.01)
            assert pct == pytest.approx(round(100.0 * totals[region] / grand, 2), abs=0.011)
        assert sum(r[2] for r in rows) == pytest.approx(100.0, abs=0.05)
        assert built.chart_hint.type == "pie" and "share_pct" in " ".join(built.assumptions)


def test_share_needs_an_additive_measure(ctx):
    assert build_sql("share of average discount by region", ctx, today=TODAY) is None


def test_growth_series_month_over_month(ctx):
    for question in ("revenue growth by month", "month over month revenue", "mom revenue growth"):
        built, columns, rows = run(question, ctx)
        assert columns == ["month", "total_revenue", "growth_pct"], question
        expected = list(_by_month(golden.sales_rows()).values())
        assert [r[1] for r in rows] == pytest.approx(expected, abs=0.01)
        assert rows[0][2] is None
        for i in range(1, len(rows)):
            assert rows[i][2] == pytest.approx(round(100.0 * (expected[i] - expected[i - 1]) / expected[i - 1], 2),
                                               abs=0.011)
        assert built.chart_hint.type == "line"


def test_growth_this_year_vs_last_year(ctx):
    built, columns, rows = run("revenue growth this year vs last year", ctx)
    assert columns == ["current_period", "previous_period", "change_pct"]
    cur = sum(r["revenue"] or 0 for r in golden.sales_rows() if r["order_date"] >= "2026-01-01")
    prev = sum(r["revenue"] or 0 for r in golden.sales_rows() if "2025-01-01" <= r["order_date"] < "2026-01-01")
    [(got_cur, got_prev, pct)] = rows
    assert (got_cur, got_prev) == (pytest.approx(cur, abs=0.01), pytest.approx(prev, abs=0.01))
    assert pct == pytest.approx(round(100.0 * (cur - prev) / prev, 2), abs=0.011)
    assert "change_pct" in built.assumptions[-1]


def test_average_per_entity(ctx):
    rows = golden.sales_rows()
    built, columns, result = run("average revenue per order", ctx)
    orders = {r["order_id"] for r in rows}
    assert columns == ["revenue_per_order"]
    assert result[0][0] == pytest.approx(sum(r["revenue"] or 0 for r in rows) / len(orders), rel=1e-9)
    assert "SUM(revenue) / COUNT(DISTINCT order_id)" in built.assumptions[0]

    _, columns, result = run("revenue per customer by region", ctx)
    assert columns == ["region", "revenue_per_customer"]
    rev, cust = defaultdict(float), defaultdict(set)
    for r in rows:
        rev[r["region"]] += r["revenue"] or 0.0
        cust[r["region"]].add(r["customer_id"])
    for region, value in result:
        assert value == pytest.approx(rev[region] / len(cust[region]), rel=1e-9)
    assert [v for _, v in result] == sorted((v for _, v in result), reverse=True)


def test_two_dimensions(ctx):
    built, columns, result = run("revenue by region and channel", ctx)
    assert columns == ["region", "channel", "total_revenue"]
    want = defaultdict(float)
    for r in golden.sales_rows():
        want[(r["region"], r["channel"])] += r["revenue"] or 0.0
    assert {(a, b): v for a, b, v in result} == pytest.approx(dict(want), abs=0.01)
    assert built.chart_hint.series == "channel"


# ------------------------------------------------------------------------------------------------ safety
def test_masked_columns_are_never_filtered_on(ctx_values):
    masked = replace(ctx_values, policy=replace(
        ctx_values.policy, column_masks={("sales", "region"): MaskSpec(kind="mask_null")}))
    assert build_sql("revenue in the north region", masked, today=TODAY) is None
    assert build_sql("top 3 products by revenue in the west", masked, today=TODAY) is None
    # other columns still filter normally
    assert build_sql("total revenue from electronics", masked, today=TODAY) is not None


def test_row_policies_disable_known_values_but_keep_named_values(ctx_values):
    policy = replace(ctx_values.policy, row_predicates={"sales": RowPredicate(conditions=(("region", "=", "North"),))})
    limited = replace(ctx_values, policy=policy)
    assert build_sql("total revenue from electronics", limited, today=TODAY) is None
    built = build_sql("revenue in the north region", limited, today=TODAY)
    assert built is not None and "LOWER(\"region\") = 'north'" in built.sql


def test_pii_columns_are_never_filtered_on(ctx):
    meta = tuple({**m, "values": ["Asha Rao"]} if m["name"] == "customer_name" else m
                 for m in ctx.columns_meta["sales"])
    c = replace(ctx, columns_meta={"sales": meta})
    assert build_sql("revenue from asha rao", c, today=TODAY) is None


def test_ambiguous_and_multiple_values_are_declined(ctx):
    meta = tuple({**m, "values": ["North", "Prime"]} if m["name"] in ("region", "channel") else m
                 for m in ctx.columns_meta["sales"])
    c = replace(ctx, columns_meta={"sales": meta})
    assert build_sql("total revenue from prime", c, today=TODAY) is None  # region or channel?
    built = build_sql("total revenue from prime channel", c, today=TODAY)  # the column name disambiguates
    assert built is not None and "\"channel\" = 'Prime'" in built.sql
    vals = apply_values(ctx, {"region": ("North", "South")})
    assert build_sql("revenue in the north region and the south region", vals, today=TODAY) is None
