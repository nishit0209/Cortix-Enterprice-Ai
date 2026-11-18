"""SQL guard validation: one test (or parametrized group) per rule of SECURITY_ARCHITECTURE §5.1. No DB."""

from __future__ import annotations

import pytest

from apps.sqlguard.services import validate
from nexora.contracts import GUARD_REJECT_CODES

from .policies import make_policy

POLICY = make_policy()


def rejected(sql: str, code: str, policy=POLICY):
    result = validate(sql, policy)
    assert result.ok is False, f"accepted: {result.executed_sql}"
    assert result.reject_code == code, (result.reject_code, result.message)
    assert result.executed_sql is None and dict(result.params) == {}
    assert result.reject_code in GUARD_REJECT_CODES
    return result


def accepted(sql: str, policy=POLICY):
    result = validate(sql, policy)
    assert result.ok is True, (result.reject_code, result.message)
    return result


# --- 1. size ------------------------------------------------------------------------------------------------
def test_text_over_10kb_is_too_large():
    rejected("SELECT region FROM sales WHERE product = '" + "x" * 11_000 + "'", "too_large")


def test_more_than_1500_nodes_is_too_large():
    values = ", ".join(str(i) for i in range(900))
    rejected(f"SELECT region FROM sales WHERE order_id IN ({values}) OR quantity IN ({values})", "too_large")


def test_nesting_deeper_than_12_is_too_large():
    sql = "SELECT region FROM sales"
    for i in range(13):
        sql = f"SELECT region FROM ({sql}) AS n{i}"
    rejected(sql, "too_large")


def test_nesting_of_12_is_accepted():
    sql = "SELECT region FROM sales"
    for i in range(11):
        sql = f"SELECT region FROM ({sql}) AS n{i}"
    accepted(sql)


def test_deep_parentheses_are_too_large_not_a_crash():
    rejected("SELECT " + "(" * 500 + "1" + ")" * 500, "too_large")


# --- 2. parsing ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", ["", "   ", ";", "SELECT", "SELECT (", "))))", "--", "/*", "$$", "SELECT '\x00'",
                                 "SELEC region FROM sales", 123])
def test_garbage_is_a_parse_error(sql):
    result = validate(sql, POLICY)
    assert result.ok is False and result.reject_code in ("parse_error", "too_large")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $tag$x$tag$ AS a, region FROM sales",
        "SELECT $$x$$ AS a FROM sales",
        "SELECT E'abc' AS a FROM sales",
        "SELECT U&'\\0061' AS a FROM sales",
        "SELECT B'101' AS a FROM sales",
    ],
)
def test_exotic_string_syntax_is_refused(sql):
    rejected(sql, "parse_error")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE sales",
        "SELECT region FROM sales; SELECT email FROM customers",
        "SELECT 1 /* ; */ ; DELETE FROM sales",
        "BEGIN; SELECT region FROM sales; COMMIT",
        "SELECT region FROM sales WHERE region = 'North'\r\n; DELETE FROM sales",
    ],
)
def test_more_than_one_statement(sql):
    rejected(sql, "multiple_statements")


def test_a_trailing_semicolon_is_fine():
    accepted("SELECT region FROM sales;")


# --- 3. statement type --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales (order_id) VALUES (1)",
        "UPDATE sales SET revenue = 0",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "TRUNCATE sales",
        "CREATE TABLE x AS SELECT * FROM sales",
        "COPY sales TO '/tmp/x.csv'",
        "SET ROLE nexora_ops",
        "RESET ROLE",
        "CALL refresh()",
        "DO $$ BEGIN END $$",
        "PREPARE p AS SELECT region FROM sales",
        "WITH d AS (DELETE FROM sales RETURNING *) SELECT * FROM d",
        "SELECT * INTO sales_copy FROM sales",
        "ALTER TABLE sales ADD COLUMN x int",
    ],
)
def test_only_select_is_allowed(sql):
    result = validate(sql, POLICY)
    assert result.ok is False and result.reject_code in ("not_select", "parse_error")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM sales UNION SELECT city FROM customers",
        "SELECT region FROM sales INTERSECT SELECT city FROM customers",
        "SELECT region FROM sales EXCEPT SELECT city FROM customers",
        "WITH t AS (SELECT region FROM sales) SELECT region FROM t",
        "(SELECT region FROM sales)",
    ],
)
def test_set_operations_and_ctes_are_selects(sql):
    accepted(sql)


# --- 4. clauses ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region FROM sales FOR UPDATE",
        "SELECT region FROM sales FOR SHARE",
        "SELECT region FROM sales FOR NO KEY UPDATE SKIP LOCKED",
        "SELECT region FROM sales WHERE order_id = $1",
        "SELECT region FROM sales WHERE order_id = %(p1)s",
        "SELECT region FROM sales WHERE order_id = %s",
        "SELECT region FROM sales TABLESAMPLE SYSTEM (10)",
    ],
)
def test_forbidden_clauses(sql):
    rejected(sql, "forbidden_clause")


def test_recursive_cte():
    rejected("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r", "recursive_cte")


def test_percent_in_identifier_is_refused():
    rejected('SELECT region AS "50%" FROM sales', "forbidden_clause")


# --- 5-6. functions ------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(10)",
        "SELECT region FROM sales WHERE pg_sleep(1) IS NOT NULL",
        "SELECT PG_SLEEP(1) FROM sales",
        'SELECT "pg_sleep"(1)',
        "SELECT pg_catalog.pg_sleep(1)",
        "SELECT pg_catalog.lower(region) FROM sales",
        "SELECT set_config('app.org_id', 'x', true)",
        "SELECT current_setting('app.org_id')",
        "SELECT * FROM dblink('host=10.0.0.5', 'select 1') AS t(a int)",
        "SELECT query_to_xml('select 1', true, true, '')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT version()",
        "SELECT inet_server_addr()",
        "SELECT txid_current()",
        "SELECT pg_backend_pid()",
        "SELECT * FROM generate_series(1, 10)",
        "SELECT region FROM sales s, LATERAL generate_series(1, 100) g",
        "SELECT repeat('x', 1000000)",
        "SELECT lpad(region, 100000, 'x') FROM sales",
        "SELECT md5(customer_name) FROM customers",
        "SELECT encode(sha256(convert_to(customer_name, 'UTF8')), 'hex') FROM customers",
        "SELECT row_to_json(c) FROM customers c",
        "SELECT to_jsonb(c) FROM customers c",
        "SELECT unnest(ARRAY[1, 2])",
        "SELECT current_user",
        "SELECT nosuchfunction(region) FROM sales",
    ],
)
def test_functions_outside_the_allowlist(sql):
    rejected(sql, "forbidden_function")


@pytest.mark.parametrize(
    "expr",
    [
        "sum(revenue)", "avg(revenue)", "min(revenue)", "max(revenue)", "count(*)", "count(DISTINCT region)",
        "stddev(revenue)", "variance(revenue)", "percentile_cont(0.5) WITHIN GROUP (ORDER BY revenue)",
        "string_agg(region, ', ')", "array_agg(region)", "bool_and(quantity > 0)",
        "row_number() OVER (ORDER BY revenue)", "rank() OVER (ORDER BY revenue)", "lag(revenue) OVER (ORDER BY order_date)",
        "date_trunc('month', order_date)", "extract(year FROM order_date)", "date_part('year', order_date)",
        "current_date", "now()", "age(order_date)", "to_char(order_date, 'YYYY-MM')", "make_date(2024, 1, 1)",
        "coalesce(region, 'none')", "nullif(region, '')", "greatest(quantity, 1)", "least(quantity, 1)",
        "CASE WHEN revenue > 10 THEN 'big' ELSE 'small' END", "lower(region)", "upper(region)", "trim(region)",
        "length(region)", "substring(region FROM 1 FOR 2)", "position('a' IN region)", "replace(region, 'a', 'b')",
        "concat(region, product)", "left(region, 2)", "right(region, 2)", "split_part(region, ' ', 1)",
        "round(revenue, 2)", "floor(revenue)", "ceil(revenue)", "abs(revenue)", "power(revenue, 2)", "sqrt(revenue)",
        "ln(revenue)", "log(revenue)", "mod(quantity, 2)", "quantity % 2", "revenue::numeric(12, 2)",
        "CAST(quantity AS text)", "order_date + interval '1 day'", "meta ->> 'a'", "region ILIKE 'n%'",
        "region IS DISTINCT FROM product", "region IN ('North', 'South')", "quantity BETWEEN 1 AND 5",
    ],
)
def test_allowlisted_functions_and_operators_are_accepted(expr):
    accepted(f"SELECT {expr} AS x FROM sales")


# --- 7. casts -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    ["SELECT 'pg_authid'::regclass", "SELECT region::regtype FROM sales", "SELECT quantity::oid FROM sales",
     "SELECT region::json FROM sales", "SELECT quantity::int[] FROM sales"],
)
def test_casts_outside_the_type_allowlist(sql):
    rejected(sql, "forbidden_cast")


# --- 8. functions in FROM ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    ["SELECT * FROM lower('x') AS t", "SELECT s.region FROM sales s, LATERAL lower(s.region) AS l"],
)
def test_functions_in_from_are_set_returning(sql):
    rejected(sql, "set_returning_function")


# --- 9. tables ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pg_class",
        "SELECT query FROM pg_stat_activity",
        "SELECT rolname FROM pg_roles",
        "SELECT * FROM pg_shadow",
        "SELECT tablename FROM pg_catalog.pg_tables",
        "SELECT table_name FROM information_schema.columns",
        "SELECT region FROM sales UNION SELECT usename FROM pg_user",
        "WITH pg_x AS (SELECT region FROM sales) SELECT region FROM pg_x",
    ],
)
def test_system_objects(sql):
    rejected(sql, "system_object")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sales_backup_2024",
        "SELECT * FROM org_b.ds_1",
        'SELECT * FROM "ORG_B"."ds_1"',
        "SELECT * FROM nexora_wh.org_b.ds_1",
        "SELECT * FROM org_unit01.ds_aaaaaaaaaaaa_v1",
        "SELECT * FROM public.orgs_membership",
        "SELECT region FROM sales WHERE order_id IN (SELECT order_id FROM org_b.ds_1)",
        "WITH sales AS (SELECT region FROM sales) SELECT region FROM sales",
        'SELECT * FROM "Sales"',
        "WITH t AS (SELECT 1 AS a) SELECT a FROM (SELECT a FROM t) x, other_table",
    ],
)
def test_tables_outside_the_policy(sql):
    rejected(sql, "table_not_allowed")


def test_only_tables_of_the_policy_are_visible():
    policy = make_policy(allowed={"sales": frozenset({"region"})})
    rejected("SELECT city FROM customers", "table_not_allowed", policy)


def test_unquoted_table_names_are_case_insensitive():
    accepted("SELECT REGION FROM SALES")


def test_cte_out_of_scope_is_not_resolved():
    rejected("SELECT a FROM (WITH t AS (SELECT region AS a FROM sales) SELECT a FROM t) x, t", "table_not_allowed")


# --- 10. columns --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers",
        "SELECT customers.email FROM customers",
        'SELECT "email" FROM customers',
        'SELECT "EMAIL" FROM customers',
        "SELECT customer_name FROM customers WHERE email LIKE 'a%'",
        "SELECT customer_name FROM customers ORDER BY email",
        "SELECT count(*) FROM customers GROUP BY email",
        "SELECT city, count(*) FROM customers GROUP BY city HAVING max(email) > 'a'",
        "SELECT CASE WHEN email LIKE '%@x' THEN 1 ELSE 0 END AS f FROM customers",
        "SELECT max(length(email)) FROM customers",
        "SELECT customer_name FROM customers c WHERE EXISTS (SELECT email FROM customers)",
        "SELECT s.region FROM sales s JOIN customers c ON c.email = s.region",
        "SELECT s.region FROM sales s JOIN customers c USING (email)",
        "SELECT c FROM customers c",
        "SELECT customers FROM customers",
        "SELECT city FROM sales NATURAL JOIN customers",
        "SELECT ctid, region FROM sales",
        "SELECT xmin FROM sales",
        "SELECT tableoid::regclass FROM sales",
        "SELECT region FROM sales WHERE customer_id IN (SELECT customer_id FROM customers WHERE email = 'x')",
        "WITH t AS (SELECT email FROM customers) SELECT * FROM t",
    ],
)
def test_denied_columns_anywhere(sql):
    rejected(sql, "column_not_allowed")


def test_ambiguous_column():
    rejected("SELECT customer_id FROM sales JOIN customers ON sales.customer_id = customers.customer_id",
             "ambiguous_column")


def test_unknown_alias():
    rejected("SELECT x.region FROM sales s", "unknown_table")


def test_star_expands_to_allowed_columns_only():
    result = accepted("SELECT * FROM customers")
    assert "email" not in result.executed_sql
    assert '"customer_name"' in result.executed_sql


def test_qualified_star_expands_to_allowed_columns_only():
    result = accepted("SELECT c.* FROM customers c")
    assert "email" not in result.executed_sql


def test_order_by_output_alias_is_allowed():
    accepted("SELECT region AS r, sum(revenue) AS total FROM sales GROUP BY region ORDER BY total DESC, r")


def test_reject_codes_are_contract_codes_for_a_wide_sample():
    samples = ["SELECT", "DROP TABLE sales", "SELECT pg_sleep(1)", "SELECT * FROM pg_class",
               "SELECT email FROM customers", "SELECT 'x'::regclass", "SELECT 1; SELECT 2", "SELECT * FROM nope"]
    for sql in samples:
        result = validate(sql, POLICY)
        assert not result.ok and result.reject_code in GUARD_REJECT_CODES, sql
