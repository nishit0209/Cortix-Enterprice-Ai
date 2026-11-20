"""SQL guard attack corpus (blocking once apps.sqlguard ships validate(); P4-AC1).

Structure tests always run. Guard tests skip with a clear reason until the Data Engineering agent ships
`apps.sqlguard.services.validate(sql, policy) -> GuardResult` (wave 2). Expected reject codes are the W2 contract
set `nexora.contracts.GUARD_REJECT_CODES`; a reject code outside that set fails the corpus.
"""

from __future__ import annotations

from collections import Counter

import pytest

from nexora.contracts import GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES
from testing.sqlguard import (
    KNOWN_CODES,
    SECURITY_CODES,
    build_policy,
    case_sql,
    get_validate,
    load_corpus,
    oracle_violations,
    policy_spec,
)

CORPUS = load_corpus()
CASES = CORPUS["cases"]
VALIDATE = get_validate()
needs_guard = pytest.mark.skipif(
    VALIDATE is None,
    reason="apps.sqlguard.services.validate not implemented yet (Data Engineering, wave 2)",
)


# ------------------------------------------------------------------------------------------- corpus structure
def test_corpus_has_at_least_60_cases_in_every_category():
    assert len(CASES) >= 60
    counts = Counter(c["category"] for c in CASES)
    assert set(counts) == set(CORPUS["categories"]), counts
    assert all(n >= 3 for n in counts.values()), counts


def test_corpus_cases_are_well_formed():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in CASES:
        assert ("sql" in c) != ("generate" in c), c["id"]
        assert c["expect"] in ("reject", "neutralized", "safe"), c["id"]
        assert c.get("policy", "default") in CORPUS["policies"], c["id"]
        if c["expect"] == "reject":
            assert c.get("codes"), f"{c['id']}: reject cases need expected codes"
            assert set(c["codes"]) <= GUARD_REJECT_CODES, (
                f"{c['id']}: codes outside the contract {set(c['codes']) - GUARD_REJECT_CODES}"
            )
            assert len(c["codes"]) == len(set(c["codes"])), f"{c['id']}: duplicate codes"
        assert c.get("stage", "validate") in ("validate", "execute"), c["id"]
        assert case_sql(c).strip()


def test_policies_build_valid_query_policies():
    for name in CORPUS["policies"]:
        spec = policy_spec(name)
        policy = build_policy(name)
        assert set(policy.tables) == set(spec["allowed_columns"])
        for table, denied in spec["denied_columns"].items():
            assert not set(denied) & set(policy.allowed_columns[table]), f"{name}.{table}"


def test_known_codes_are_exactly_the_contract_codes():
    assert KNOWN_CODES == GUARD_REJECT_CODES
    assert SECURITY_CODES == GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES
    assert REPAIRABLE_REJECT_CODES <= GUARD_REJECT_CODES


def test_attack_functions_and_catalogs_expect_terminal_codes():
    """Calling a dangerous function or reading a system object must never be 'repairable' (no LLM retry)."""
    for c in CASES:
        if c["expect"] != "reject":
            continue
        if c["category"] == "dangerous_functions" or c["codes"] == ["system_object"]:
            assert not set(c["codes"]) & REPAIRABLE_REJECT_CODES, c["id"]


def test_w2_corpus_additions_are_present():
    counts = Counter(c["category"] for c in CASES)
    for category in (
        "deny_all_predicate",
        "mask_bypass",
        "cte_shadowing",
        "set_operation",
        "unicode_identifiers",
    ):
        assert counts[category] >= 5, (category, counts[category])
    ids = {c["id"] for c in CASES}
    assert {"inject-005", "inject-006", "inject-007", "inject-008"} <= ids
    deny = build_policy("deny_all")
    assert deny.row_predicates["sales"].conditions == (("region", "in", ()),)
    assert build_policy("allow_all_notin").row_predicates["sales"].conditions == (("region", "not_in", ()),)
    assert {m.kind for m in build_policy("masked_multi").column_masks.values()} == {
        "mask_partial",
        "mask_hash",
        "mask_null",
    }


def test_generated_inputs_are_really_large():
    by_id = {c["id"]: c for c in CASES}
    assert case_sql(by_id["dos-003"]).count("SELECT") == 21
    assert case_sql(by_id["dos-004"]).count(",") == 9999
    assert len(case_sql(by_id["dos-005"]).encode()) > 10 * 1024


def _accepted(sql: str, **params):
    from nexora.contracts import GuardResult

    return GuardResult(ok=True, reject_code=None, message="", executed_sql=sql, params=params)


def test_oracle_accepts_a_correct_rewrite():
    sql = (
        'SELECT * FROM (SELECT "s"."region" AS "region" FROM (SELECT "region", "revenue" FROM '
        '"org_qatest01"."ds_101_v1" WHERE "region" = %(p1)s) AS "s") AS "_q" LIMIT 1001'
    )
    assert oracle_violations(_accepted(sql, p1="North"), "north_only", {"params_contain": ["North"]}) == []


@pytest.mark.parametrize(
    ("sql", "policy", "fragment"),
    [
        ("SELECT email FROM org_qatest01.ds_102_v1", "default", "denied column"),
        ("SELECT region FROM org_b.ds_1", "default", "table outside policy"),
        ("SELECT region FROM nexora_wh.org_qatest01.ds_101_v1", "default", "3-part"),
        ("SELECT region FROM org_qatest01.ds_101_v1", "north_only", "row policy not applied"),
        (
            "SELECT region FROM org_qatest01.ds_101_v1 WHERE revenue > 0",
            "north_only",
            "row policy not applied",
        ),
        (
            "SELECT region FROM (SELECT region FROM org_qatest01.ds_101_v1 WHERE region = %(p1)s) a "
            "UNION ALL SELECT region FROM org_qatest01.ds_101_v1",
            "north_only",
            "row policy not applied",
        ),
        ("SELECT pg_class.relname FROM pg_class", "default", "table outside policy"),
    ],
)
def test_oracle_flags_unsafe_rewrites(sql, policy, fragment):
    problems = oracle_violations(_accepted(sql, p1="North"), policy)
    assert any(fragment in p for p in problems), problems


@pytest.mark.parametrize(
    ("sql", "policy", "fragment"),
    [
        ("SELECT 1; DROP TABLE org_qatest01.ds_101_v1", "default", "2 statements"),
        ("DELETE FROM org_qatest01.ds_101_v1", "default", "not a SELECT"),
        (
            "SELECT region FROM org_qatest01.ds_101_v1 WHERE region IN (NULL)",
            "deny_all",
            "not rendered as FALSE",
        ),
        (
            "SELECT phone FROM (SELECT phone FROM org_qatest01.ds_102_v1) AS c",
            "masked_multi",
            "read raw",
        ),
        (
            "SELECT c.customer_id FROM (SELECT customer_id, repeat('*', 4) || right(phone, 4) AS phone "
            "FROM org_qatest01.ds_102_v1 ORDER BY phone) AS c",
            "masked_multi",
            "used raw in ORDER",
        ),
        (
            "SELECT c.customer_id FROM (SELECT customer_id FROM org_qatest01.ds_102_v1 "
            "WHERE phone = '9800000001') AS c",
            "masked_multi",
            "filtered raw in WHERE",
        ),
        (
            "SELECT c.city FROM (SELECT city AS city FROM org_qatest01.ds_102_v1) AS c",
            "masked_multi",
            "read raw",
        ),
        ("SELECT pg_sleep(1) FROM org_qatest01.ds_101_v1", "default", "system function"),
    ],
)
def test_oracle_flags_w2_violations(sql, policy, fragment):
    problems = oracle_violations(_accepted(sql), policy)
    assert any(fragment in p for p in problems), problems


def test_oracle_accepts_correct_deny_all_and_mask_rewrites():
    deny = (
        'SELECT * FROM (SELECT "s"."region" AS "region" FROM (SELECT "region" FROM "org_qatest01"."ds_101_v1" '
        'WHERE FALSE) AS "s") AS "_q" LIMIT 1001'
    )
    assert oracle_violations(_accepted(deny), "deny_all", {"contains_ci": ["false"]}) == []
    masked = (
        "SELECT * FROM (SELECT c.customer_id, c.phone, c.customer_name, c.city FROM (SELECT customer_id, "
        "repeat('*', greatest(length(phone) - 4, 0)) || right(phone, 4) AS phone, "
        "encode(sha256(convert_to(customer_name || %(salt)s, 'UTF8')), 'hex') AS customer_name, "
        "CAST(NULL AS TEXT) AS city FROM org_qatest01.ds_102_v1) AS c ORDER BY c.phone) AS _q LIMIT 1001"
    )
    assert oracle_violations(_accepted(masked, salt="s"), "masked_multi") == []


# W3: the approved mask_null rendering (SECURITY_ARCHITECTURE §5 table, CONTRACT "W2 as-built additions").
_MASKED_SOURCE = (
    "SELECT * FROM (SELECT c.customer_id, c.city FROM (SELECT customer_id, {city} AS city "
    "FROM org_qatest01.ds_102_v1 AS _src) AS c) AS _q LIMIT 1001"
)


@pytest.mark.parametrize(
    "city",
    [
        'CASE WHEN FALSE THEN "_src"."city" END',
        "CASE WHEN false THEN city END",
        "CAST(NULL AS TEXT)",
        "NULL",
    ],
)
def test_oracle_accepts_the_approved_mask_null_renderings(city):
    assert oracle_violations(_accepted(_MASKED_SOURCE.format(city=city)), "masked_multi") == []


@pytest.mark.parametrize(
    "city",
    [
        "CASE WHEN TRUE THEN city END",
        "CASE WHEN 1 = 0 THEN city END",
        "CASE WHEN NOT TRUE THEN city END",
        "CASE WHEN FALSE THEN city ELSE city END",
        "CASE WHEN FALSE THEN NULL ELSE city END",
        "CASE WHEN FALSE THEN city WHEN TRUE THEN city END",
        "CASE WHEN customer_id > 0 THEN city END",
        "CASE city WHEN FALSE THEN city END",
        "COALESCE(CASE WHEN FALSE THEN city END, city)",
        "CASE WHEN FALSE THEN city END || city",
        "city",
        "upper(city)",
    ],
)
def test_oracle_flags_every_other_mask_null_shape(city):
    problems = oracle_violations(_accepted(_MASKED_SOURCE.format(city=city)), "masked_multi")
    assert any("city" in p and "raw" in p for p in problems), (city, problems)


def test_oracle_mask_null_exemption_is_per_reference_in_every_clause():
    """The approved wrapper is safe wherever it sits; a raw sibling reference is still caught."""
    ok = (
        "SELECT c.customer_id FROM (SELECT customer_id, CASE WHEN FALSE THEN city END AS city "
        "FROM org_qatest01.ds_102_v1 ORDER BY CASE WHEN FALSE THEN city END) AS c"
    )
    assert oracle_violations(_accepted(ok), "masked_multi") == []
    leaky = (
        "SELECT c.customer_id FROM (SELECT customer_id, CASE WHEN FALSE THEN city END AS city "
        "FROM org_qatest01.ds_102_v1 ORDER BY city) AS c"
    )
    assert any("used raw in ORDER" in p for p in oracle_violations(_accepted(leaky), "masked_multi"))
    filtered = (
        "SELECT c.customer_id FROM (SELECT customer_id, CASE WHEN FALSE THEN city END AS city "
        "FROM org_qatest01.ds_102_v1 WHERE city = 'Pune') AS c"
    )
    assert any("filtered raw in WHERE" in p for p in oracle_violations(_accepted(filtered), "masked_multi"))


def test_oracle_mask_null_wrapper_does_not_excuse_other_mask_kinds():
    """`CASE WHEN FALSE THEN phone END` is not a mask_partial rendering: phone must carry repeat()/right()."""
    sql = "SELECT c.phone FROM (SELECT CASE WHEN FALSE THEN phone END AS phone FROM org_qatest01.ds_102_v1) AS c"
    assert any("phone" in p and "read raw" in p for p in oracle_violations(_accepted(sql), "masked_multi"))


# ------------------------------------------------------------------------------------------- guard verdicts
def _params():
    for c in CASES:
        marks = [pytest.mark.ac("P4-AC1")]
        if c.get("stage") == "execute":
            marks.append(
                pytest.mark.skip(reason="decided at execution (EXPLAIN); covered by the warehouse suite")
            )
        yield pytest.param(c, id=c["id"], marks=marks)


@needs_guard
@pytest.mark.parametrize("case", list(_params()))
def test_guard_verdict(case):
    policy_name = case.get("policy", "default")
    result = VALIDATE(case_sql(case), build_policy(policy_name))
    expect = case["expect"]
    if not result.ok:
        assert result.reject_code in GUARD_REJECT_CODES, (
            f"{case['id']}: non-contract code {result.reject_code!r}"
        )
    if expect == "reject":
        assert result.ok is False, f"{case['id']} accepted: {result.executed_sql}"
        assert result.reject_code in case["codes"], (
            f"{case['id']}: got {result.reject_code}, want {case['codes']}"
        )
        assert result.executed_sql in (None, ""), "a rejected query must not carry executable SQL"
        return
    if expect == "neutralized":
        assert result.ok is True, f"{case['id']} rejected with {result.reject_code}: {result.message}"
    if result.ok:
        assert oracle_violations(result, policy_name, case.get("checks")) == []


@needs_guard
def test_guard_never_raises_on_garbage():
    policy = build_policy("default")
    for sql in ("", " ", ";", "SELECT", "SELECT (", "))))", "\x00", "SELECT '\x00'", "--", "/*", "$$"):
        result = VALIDATE(sql, policy)
        assert result.ok is False and result.reject_code in KNOWN_CODES, repr(sql)


@needs_guard
def test_guard_is_deterministic_and_policy_bound():
    sql = "SELECT region, sum(revenue) FROM sales GROUP BY region"
    r1 = VALIDATE(sql, build_policy("default"))
    r2 = VALIDATE(sql, build_policy("default"))
    assert (r1.ok, r1.executed_sql, dict(r1.params)) == (r2.ok, r2.executed_sql, dict(r2.params))
    r3 = VALIDATE(sql, build_policy("north_only"))
    assert r3.ok and r3.executed_sql != r1.executed_sql and "North" in list(r3.params.values())
