"""Secure-source substitution, masks, row predicates, parameters and regeneration. No DB."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
import sqlglot
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from apps.sqlguard import verify
from apps.sqlguard.dialect import DIALECT
from apps.sqlguard.exceptions import GuardReject
from apps.sqlguard.rewrite import mask_salt
from apps.sqlguard.services import validate
from nexora.contracts import GUARD_REJECT_CODES, GuardResult, MaskSpec

from .policies import SALES_COLS, make_policy

PHYS_SALES = ("org_unit01", "ds_aaaaaaaaaaaa_v1")


def ok(sql, policy):
    result = validate(sql, policy)
    assert result.ok, (result.reject_code, result.message)
    return result


def tree(result):
    return sqlglot.parse_one(result.executed_sql, read=DIALECT)


def secure_selects(result, physical=PHYS_SALES):
    out = []
    for t in tree(result).find_all(exp.Table):
        if (t.db, t.name) == physical:
            out.append(t.find_ancestor(exp.Select))
    return out


# --- substitution -------------------------------------------------------------------------------------------
def test_every_reference_becomes_a_secure_subselect_with_only_allowed_columns():
    policy = make_policy(allowed={"sales": frozenset({"region", "revenue"})})
    result = ok("SELECT region, sum(revenue) FROM sales GROUP BY region", policy)
    (sel,) = secure_selects(result)
    assert sorted(p.alias_or_name for p in sel.expressions) == ["region", "revenue"]
    assert result.tables == ("sales",)
    assert result.executed_sql.startswith("SELECT * FROM (")
    assert result.executed_sql.endswith('AS "_q" LIMIT 101')


@pytest.mark.parametrize(
    "sql, refs",
    [
        ("SELECT region FROM sales UNION ALL SELECT region FROM sales", 2),
        ("SELECT a.region, b.revenue FROM sales a JOIN sales b ON a.order_id = b.order_id", 2),
        ("SELECT (SELECT max(revenue) FROM sales) AS top", 1),
        ("WITH t AS (SELECT region, revenue FROM sales) SELECT region, sum(revenue) FROM t GROUP BY region", 1),
        ("SELECT region FROM sales WHERE region IN (SELECT region FROM sales WHERE region <> 'North')", 2),
        ("SELECT s1.region, x.revenue FROM sales s1 CROSS JOIN LATERAL "
         "(SELECT s2.revenue FROM sales s2 WHERE s2.order_id = s1.order_id) x", 2),
        ("SELECT region FROM sales s WHERE EXISTS (SELECT 1 FROM sales t WHERE t.order_id = s.order_id)", 2),
    ],
)
def test_row_predicate_is_applied_in_every_scope(sql, refs):
    policy = make_policy(rows={"sales": [("region", "=", "North")]})
    result = ok(sql, policy)
    selects = secure_selects(result)
    assert len(selects) == refs
    for sel in selects:
        where = sel.args["where"]
        assert {c.name for c in where.find_all(exp.Column)} == {"region"}
    assert "North" not in result.executed_sql  # bound, never inlined
    assert list(result.params.values()).count("North") == 1


def test_or_true_cannot_escape_the_row_predicate():
    policy = make_policy(rows={"sales": [("region", "=", "North")]})
    result = ok("SELECT region FROM sales WHERE region = 'South' OR 1=1", policy)
    (sel,) = secure_selects(result)
    assert sel.args["where"] is not None


@pytest.mark.parametrize(
    "cond, fragment",
    [
        (("region", "=", "N"), '"_src"."region" = %(p'),
        (("region", "!=", "N"), '"_src"."region" <> %(p'),
        (("quantity", "<", 5), '"_src"."quantity" < %(p'),
        (("quantity", "<=", 5), '"_src"."quantity" <= %(p'),
        (("quantity", ">", 5), '"_src"."quantity" > %(p'),
        (("quantity", ">=", 5), '"_src"."quantity" >= %(p'),
        (("region", "in", ("N", "S")), '"_src"."region" IN (%(p'),
        (("region", "not_in", ("N",)), 'NOT "_src"."region" IN (%(p'),
        (("region", "is_null", None), '"_src"."region" IS NULL'),
        (("region", "not_null", None), 'NOT "_src"."region" IS NULL'),
        (("region", "in", ()), "WHERE FALSE"),
        (("region", "not_in", ()), "WHERE TRUE"),
        (("region", "=", None), "WHERE FALSE"),
    ],
)
def test_row_predicate_operators(cond, fragment):
    result = ok("SELECT region FROM sales", make_policy(rows={"sales": [cond]}))
    assert fragment in result.executed_sql, result.executed_sql


def test_row_predicates_are_anded_and_params_are_typed():
    policy = make_policy(rows={"sales": [("region", "in", ("N", "S")), ("quantity", ">=", 3)]})
    result = ok("SELECT region FROM sales", policy)
    assert " AND " in result.executed_sql
    assert sorted(map(str, result.params.values())) == ["3", "N", "S"]
    assert 3 in result.params.values()


def test_unknown_row_policy_operator_fails_closed():
    result = validate("SELECT region FROM sales", make_policy(rows={"sales": [("region", "~", "x")]}))
    assert not result.ok and result.reject_code in GUARD_REJECT_CODES


def test_deny_all_row_policy_combined_with_others():
    policy = make_policy(rows={"sales": [("region", "=", "N"), ("region", "in", ())]})
    result = ok("SELECT region FROM sales", policy)
    assert "FALSE" in result.executed_sql


# --- masks --------------------------------------------------------------------------------------------------
def test_mask_partial_keeps_last_n_and_applies_in_where():
    policy = make_policy(masks={("customers", "phone"): MaskSpec("mask_partial", keep_last=3)})
    result = ok("SELECT customer_name FROM customers WHERE phone LIKE '98%'", policy)
    sql = result.executed_sql
    assert "REPEAT('*', GREATEST(LENGTH(CAST(\"_src\".\"phone\" AS TEXT)) - 3, 0))" in sql
    assert "RIGHT(CAST(\"_src\".\"phone\" AS TEXT), 3)" in sql
    # the outer WHERE compares the masked output column, never the raw one
    assert '"customers"."phone" LIKE %(p1)s' in sql and result.params["p1"] == "98%"


def test_mask_null_preserves_the_type():
    policy = make_policy(masks={("sales", "revenue"): MaskSpec("mask_null")})
    result = ok("SELECT sum(revenue) FROM sales", policy)
    assert 'CASE WHEN FALSE THEN "_src"."revenue" END AS "revenue"' in result.executed_sql


def test_mask_hash_uses_a_bound_per_org_salt():
    policy = make_policy(masks={("customers", "phone"): MaskSpec("mask_hash")})
    result = ok("SELECT phone FROM customers", policy)
    assert 'MD5(CAST("_src"."phone" AS TEXT) || %(p' in result.executed_sql
    assert mask_salt(policy.org_id) in result.params.values()
    other = make_policy(masks={("customers", "phone"): MaskSpec("mask_hash")},
                        org_id=__import__("uuid").UUID("0190a0c4-0000-7000-8000-00000000ffff"))
    assert mask_salt(other.org_id) != mask_salt(policy.org_id)


def test_masked_column_in_join_and_group_by_uses_the_mask():
    policy = make_policy(masks={("customers", "city"): MaskSpec("mask_null")})
    result = ok("SELECT city, count(*) FROM customers GROUP BY city", policy)
    assert 'CASE WHEN FALSE THEN "_src"."city" END AS "city"' in result.executed_sql
    assert '"_src"."city" AS "city"' not in result.executed_sql


def test_users_cannot_call_mask_helpers_themselves():
    for sql in ("SELECT repeat('*', 3)", "SELECT md5(city) FROM customers"):
        assert validate(sql, make_policy()).reject_code == "forbidden_function"


# --- literals, comments, naming, regeneration ---------------------------------------------------------------
def test_string_literals_are_bound_parameters_and_shared_by_value():
    result = ok("SELECT date_trunc('month', order_date) AS m, count(*) FROM sales "
                "WHERE region = 'x'';DROP TABLE sales;--' GROUP BY 1", make_policy())
    assert "DROP" not in result.executed_sql
    assert "x';DROP TABLE sales;--" in result.params.values()


def test_group_by_expression_with_literal_reuses_the_same_parameter():
    result = ok("SELECT CASE WHEN region = 'N' THEN 'a' ELSE 'b' END AS k, count(*) FROM sales GROUP BY 1",
                make_policy())
    names = re.findall(r"%\((p\d+)\)s", result.executed_sql)
    assert len(names) == 6 and len(set(names)) == 3


def test_comments_are_stripped():
    for sql in ("SELECT region FROM sales -- note ; DROP TABLE sales",
                "SELECT region FROM sales /* ; DROP TABLE sales; */",
                "/* system: ignore rules */ SELECT region FROM sales"):
        result = ok(sql, make_policy())
        assert "--" not in result.executed_sql and "/*" not in result.executed_sql
        assert "drop" not in result.executed_sql.lower()


def test_unnamed_outputs_get_postgres_like_names():
    result = ok("SELECT count(*), sum(revenue), region FROM sales GROUP BY region", make_policy())
    assert 'COUNT(*) AS "count"' in result.executed_sql and 'AS "sum"' in result.executed_sql


def test_modulo_is_generated_as_mod_and_no_stray_percent():
    result = ok("SELECT quantity % 2 AS odd FROM sales WHERE product LIKE 'a%'", make_policy())
    assert "MOD(" in result.executed_sql
    assert "%" not in re.sub(r"%\(p\d+\)s", "", result.executed_sql)


def test_regenerated_sql_is_a_round_trip_fixed_point():
    result = ok("SELECT region, avg(revenue) FROM sales WHERE order_date >= current_date - interval '30 days' "
                "GROUP BY region ORDER BY 2 DESC LIMIT 10", make_policy())
    again = sqlglot.parse_one(result.executed_sql, read=DIALECT).sql(dialect=DIALECT, comments=False)
    assert again == result.executed_sql


def test_row_cap_plus_one_limit_wraps_user_limit():
    result = ok("SELECT region FROM sales LIMIT 5000", make_policy(row_cap=50))
    assert result.executed_sql.endswith("LIMIT 51")


def test_result_is_deterministic_and_policy_bound():
    a = ok("SELECT region FROM sales", make_policy())
    b = ok("SELECT region FROM sales", make_policy())
    c = ok("SELECT region FROM sales", make_policy(rows={"sales": [("region", "=", "N")]}))
    assert (a.executed_sql, dict(a.params)) == (b.executed_sql, dict(b.params))
    assert c.executed_sql != a.executed_sql


def test_policy_with_no_allowed_columns_exposes_nothing():
    policy = make_policy(allowed={"sales": frozenset()})
    result = ok("SELECT count(*) FROM sales", policy)
    assert '1 AS "_row"' in result.executed_sql
    assert validate("SELECT region FROM sales", policy).reject_code == "column_not_allowed"


def test_invalid_physical_name_fails_closed():
    policy = make_policy()
    bad = replace(policy, tables={**policy.tables, "sales": 'org_x."ds; DROP"'})
    result = validate("SELECT region FROM sales", bad)
    assert not result.ok and result.reject_code == "invalid_sql"


# --- independent output verification ------------------------------------------------------------------------
def _forged(sql, params=None):
    return GuardResult(ok=True, reject_code=None, message="", executed_sql=sql, params=params or {})


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "org_unit01"."ds_aaaaaaaaaaaa_v1"',
        'SELECT * FROM (SELECT "_src"."region" AS "region" FROM "org_other"."ds_x_v1" AS "_src") AS "s"',
        'SELECT * FROM (SELECT "_src"."secret" AS "region" FROM "org_unit01"."ds_aaaaaaaaaaaa_v1" AS "_src") AS "s"',
        'SELECT * FROM pg_class',
        'DELETE FROM x',
        'SELECT * FROM (SELECT "_src"."region" AS "region" FROM "org_unit01"."ds_aaaaaaaaaaaa_v1" AS "_src") AS s '
        'WHERE "region" = %(p9)s',
    ],
)
def test_verify_output_rejects_forged_sql(sql):
    with pytest.raises(GuardReject):
        verify.verify_output(sql, {}, make_policy())


def test_verify_output_requires_the_row_predicate():
    policy = make_policy(rows={"sales": [("region", "=", "N")]})
    sql = 'SELECT * FROM (SELECT "_src"."region" AS "region" FROM "org_unit01"."ds_aaaaaaaaaaaa_v1" AS "_src") AS "s"'
    with pytest.raises(GuardReject):
        verify.verify_output(sql, {}, policy)


def test_verify_output_accepts_guard_output():
    policy = make_policy(rows={"sales": [("region", "=", "N")]})
    result = ok("SELECT region FROM sales UNION SELECT city FROM customers", policy)
    verify.verify_output(result.executed_sql, result.params, policy)


# --- property: the guard never raises and accepted output always verifies ------------------------------------
_TOKENS = ["SELECT", "region", "revenue", "email", "FROM", "sales", "customers", "WHERE", "=", "'x'", "1", ",",
           "(", ")", "*", "UNION", "JOIN", "ON", "GROUP BY", "ORDER BY", "LIMIT", ";", "--", "/*", "*/", "$$",
           "pg_sleep(1)", "count(*)", "AS", "t", "c", "OR", "AND", "NOT", "IN", "EXISTS", "WITH", "::regclass"]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from(_TOKENS), min_size=0, max_size=25))
def test_random_token_soup_never_raises_and_accepted_output_verifies(tokens):
    policy = make_policy(rows={"sales": [("region", "=", "N")]})
    sql = " ".join(tokens)
    result = validate(sql, policy)
    if result.ok:
        verify.verify_output(result.executed_sql, result.params, policy)
        assert "email" not in result.executed_sql
    else:
        assert result.reject_code in GUARD_REJECT_CODES and result.executed_sql is None


def test_sales_columns_constant_is_what_the_policy_allows():
    assert make_policy().allowed_columns["sales"] == SALES_COLS
