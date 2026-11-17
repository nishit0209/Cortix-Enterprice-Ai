"""SQL guard attack corpus (blocking once apps.sqlguard ships validate(); P4-AC1).

Structure tests always run. Guard tests skip with a clear reason until the Data Engineering agent ships
`apps.sqlguard.services.validate(sql, policy) -> GuardResult` (wave 2).
"""

from __future__ import annotations

from collections import Counter

import pytest

from testing.sqlguard import (
    KNOWN_CODES,
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
            assert set(c["codes"]) <= KNOWN_CODES, f"{c['id']}: unknown codes {set(c['codes']) - KNOWN_CODES}"
        assert c.get("stage", "validate") in ("validate", "execute"), c["id"]
        assert case_sql(c).strip()


def test_policies_build_valid_query_policies():
    for name in CORPUS["policies"]:
        spec = policy_spec(name)
        policy = build_policy(name)
        assert set(policy.tables) == set(spec["allowed_columns"])
        for table, denied in spec["denied_columns"].items():
            assert not set(denied) & set(policy.allowed_columns[table]), f"{name}.{table}"


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
        ("SELECT region FROM org_qatest01.ds_101_v1 WHERE revenue > 0", "north_only", "row policy not applied"),
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


# ------------------------------------------------------------------------------------------- guard verdicts
def _params():
    for c in CASES:
        marks = [pytest.mark.ac("P4-AC1")]
        if c.get("stage") == "execute":
            marks.append(pytest.mark.skip(reason="decided at execution (EXPLAIN); covered by the warehouse suite"))
        yield pytest.param(c, id=c["id"], marks=marks)


@needs_guard
@pytest.mark.parametrize("case", list(_params()))
def test_guard_verdict(case):
    policy_name = case.get("policy", "default")
    result = VALIDATE(case_sql(case), build_policy(policy_name))
    expect = case["expect"]
    if expect == "reject":
        assert result.ok is False, f"{case['id']} accepted: {result.executed_sql}"
        assert result.reject_code in case["codes"], f"{case['id']}: got {result.reject_code}, want {case['codes']}"
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
