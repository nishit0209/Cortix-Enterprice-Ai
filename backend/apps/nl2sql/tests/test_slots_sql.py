"""Time phrases, slot extraction, SQL templating / binding and local validation."""

from __future__ import annotations

from datetime import date

import pytest

from apps.nl2sql.slots import extract_slots, normalize_question
from apps.nl2sql.sqlutil import (
    bind,
    enforce_limit,
    parse_select,
    rename_identifiers,
    render,
    shape_without_literals,
    template_slots,
    templatize,
    validate_against_context,
)
from apps.nl2sql.tests.conftest import TODAY
from apps.nl2sql.timephrases import find_periods


@pytest.mark.parametrize(("text", "start", "end"), [
    ("last month", "2026-10-01", "2026-11-01"),
    ("this month", "2026-11-01", "2026-12-01"),
    ("previous quarter", "2026-07-01", "2026-10-01"),
    ("last year", "2025-01-01", "2026-01-01"),
    ("this week", "2026-11-16", "2026-11-23"),
    ("yesterday", "2026-11-16", "2026-11-17"),
    ("today", "2026-11-17", "2026-11-18"),
    ("the last 7 days", "2026-11-11", "2026-11-18"),
    ("past 3 months", "2026-08-18", "2026-11-18"),
    ("in 2024", "2024-01-01", "2025-01-01"),
    ("in march 2026", "2026-03-01", "2026-04-01"),
    ("in december", "2025-12-01", "2026-01-01"),  # most recent December
    ("q2 2025", "2025-04-01", "2025-07-01"),
    ("ytd", "2026-01-01", "2026-11-18"),
    ("month to date", "2026-11-01", "2026-11-18"),
    ("since 15 august 2026", "2026-08-15", "2026-11-18"),
    ("between 2026-01-01 and 2026-01-31", "2026-01-01", "2026-02-01"),
    ("from 1 feb 2026 to 10 feb 2026", "2026-02-01", "2026-02-11"),
])
def test_time_phrases(text, start, end):
    [m] = find_periods(text, TODAY)
    assert m.period.start.isoformat() == start
    assert m.period.end.isoformat() == end


def test_before_is_open_ended():
    [m] = find_periods("orders before 2026-03-01", TODAY)
    assert m.period.start is None and m.period.end == date(2026, 3, 1)


def test_slots_and_template():
    s = extract_slots("Top 5 regions by revenue last month?", TODAY)
    assert s.normalized == "top 5 regions by revenue last month"
    assert s.template == "top <num> regions by revenue <period>"
    assert s.values() == {"num_0": 5, "p0_start": "2026-10-01", "p0_end": "2026-11-01"}
    assert s.shape == "n1p1"
    assert s.hash == extract_slots("top 5 regions by revenue last month", TODAY).hash
    # a different period phrase yields the same template (semantic cache re-binding)
    s2 = extract_slots("top 10 regions by revenue last quarter", TODAY)
    assert s2.template == s.template and s2.values()["num_0"] == 10


def test_numbers_inside_periods_are_not_number_slots():
    s = extract_slots("revenue in the last 30 days", TODAY)
    assert s.numbers == () and len(s.periods) == 1


def test_normalize():
    assert normalize_question("  What's   the TOTAL revenue?! ") == "what the total revenue"
    assert normalize_question("growth 2.5% in 2026-01-01") == "growth 2.5% in 2026-01-01"


SQL = ('SELECT "region", SUM("amount") AS "total_amount" FROM "sales" WHERE "order_date" >= '
       "CAST('2026-10-01' AS DATE) AND \"order_date\" < CAST('2026-11-01' AS DATE) GROUP BY \"region\" "
       'ORDER BY "total_amount" DESC LIMIT 5')


def test_templatize_and_bind_roundtrip():
    values = {"num_0": 5, "p0_start": "2026-10-01", "p0_end": "2026-11-01"}
    template, used, eq = templatize(SQL, values)
    assert set(used) == {"num_0", "p0_start", "p0_end"} and eq == []
    assert template_slots(template) == {"num_0", "p0_start", "p0_end"}
    assert "2026-10-01" not in template
    out = bind(template, {"num_0": 10, "p0_start": "2026-07-01", "p0_end": "2026-10-01"})
    assert "LIMIT 10" in out and "'2026-07-01'" in out and "'2026-10-01'" in out
    assert bind(template, values) == render(parse_select(SQL)[0])


def test_bind_refuses_missing_slots_and_broken_equalities():
    sql = ("SELECT SUM(\"amount\") FROM \"sales\" WHERE \"d\" >= '2026-10-01' AND \"d\" < '2026-11-01' "
           "AND \"d\" >= '2026-11-01'")
    values = {"p0_start": "2026-11-01", "p0_end": "2026-12-01", "p1_start": "2026-10-01", "p1_end": "2026-11-01"}
    template, used, eq = templatize(sql, values)
    assert eq == [["p0_start", "p1_end"]]
    ok = {"p0_start": "2026-11-16", "p0_end": "2026-11-23", "p1_start": "2026-11-09", "p1_end": "2026-11-16"}
    assert bind(template, ok, eq) is not None
    broken = {"p0_start": "2026-03-01", "p0_end": "2026-04-01", "p1_start": "2026-01-01", "p1_end": "2026-02-01"}
    assert bind(template, broken, eq) is None
    assert bind(template, {"p0_start": "2026-11-01"}, eq) is None


def test_sql_literal_quoting_in_bind():
    template, _, _ = templatize("SELECT * FROM \"sales\" WHERE \"region\" = 'x'", {"s": "x"})
    out = bind(template, {"s": "it's"})
    assert out is not None and "'it''s'" in out


@pytest.mark.parametrize(("sql", "code"), [
    ("", "parse_error"),
    ("SELECT FROM WHERE (", "parse_error"),
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("DELETE FROM sales", "not_select"),
    ("SELECT * INTO evil FROM sales", "forbidden_clause"),
    ("SELECT * FROM sales FOR UPDATE", "forbidden_clause"),
    ("SELECT * FROM sales WHERE id = :p", "forbidden_clause"),
])
def test_parse_select_rejections(sql, code):
    ast, issue = parse_select(sql)
    assert ast is None and issue.code == code


TABLES = {"sales": "org_x.ds_1"}
ALLOWED = {"sales": frozenset({"region", "amount", "order_date"})}


@pytest.mark.parametrize(("sql", "code"), [
    ("SELECT region FROM sales", None),
    ("SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY total", None),
    ("WITH t AS (SELECT region, amount FROM sales) SELECT region, SUM(amount) FROM t GROUP BY region", None),
    ("SELECT customer_email FROM sales", "column_not_allowed"),
    ("SELECT * FROM customers", "table_not_allowed"),
    ("SELECT * FROM pg_catalog.pg_user", "system_object"),
    ("SELECT * FROM pg_user", "system_object"),
    ("SELECT * FROM information_schema.tables", "system_object"),
    ("SELECT * FROM other_schema.sales", "table_not_allowed"),
    ("SELECT ctid FROM sales", "system_object"),
])
def test_validate_against_context(sql, code):
    ast, issue = parse_select(sql)
    assert issue is None
    problem = validate_against_context(ast, TABLES, ALLOWED)
    assert (problem.code if problem else None) == code


@pytest.mark.parametrize(("sql", "limit"), [
    ("SELECT region FROM sales", 1000), ("SELECT region FROM sales LIMIT 50", 50),
    ("SELECT region FROM sales LIMIT 50000", 1000),
])
def test_enforce_limit(sql, limit):
    ast = enforce_limit(parse_select(sql)[0])
    assert ast.args["limit"].expression.this == str(limit)


def test_rename_identifiers_and_shape():
    ast, _ = parse_select("SELECT pii_col_1, COUNT(*) FROM sales WHERE pii_col_1 LIKE '%@example.test' GROUP BY 1")
    out = render(rename_identifiers(ast, {"pii_col_1": "customer_email"}))
    assert '"customer_email"' in out and "pii_col_1" not in out
    shape = shape_without_literals(out)
    assert "example.test" not in shape and "?" in shape
