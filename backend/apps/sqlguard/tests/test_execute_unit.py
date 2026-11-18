"""execute() pre-flight refusals, EXPLAIN plan checks and metric expression validation. No DB."""

from __future__ import annotations

import uuid

import pytest

from apps.sqlguard import services
from apps.sqlguard.exceptions import GuardError, GuardReject
from nexora.contracts import GuardResult, QueryTarget

from .policies import ORG, make_policy

PLAN = [{
    "Plan": {
        "Node Type": "Aggregate", "Total Cost": 123.5,
        "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "ds_aaaaaaaaaaaa_v1", "Schema": "org_unit01", "Total Cost": 100.0},
            {"Node Type": "Hash Join", "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "ds_bbbbbbbbbbbb_v2", "Schema": "org_unit01"},
            ]},
        ],
    }
}]


def test_plan_relations_and_cost():
    assert services.plan_relations(PLAN) == {("org_unit01", "ds_aaaaaaaaaaaa_v1"), ("org_unit01", "ds_bbbbbbbbbbbb_v2")}
    assert services.plan_total_cost(PLAN) == 123.5


def test_plan_check_accepts_policy_relations_and_returns_cost():
    assert services.make_plan_check(make_policy(), max_cost=1000)(PLAN) == 123.5


def test_plan_check_refuses_foreign_relations():
    plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "pg_authid", "Schema": "pg_catalog", "Total Cost": 1}}]
    with pytest.raises(GuardError) as exc:
        services.make_plan_check(make_policy(), max_cost=1000)(plan)
    assert exc.value.reject_code == "plan_relation_not_allowed" and exc.value.status_code == 403


def test_plan_check_refuses_function_scans():
    plan = [{"Plan": {"Node Type": "Function Scan", "Function Name": "generate_series", "Total Cost": 1}}]
    with pytest.raises(GuardError) as exc:
        services.make_plan_check(make_policy(), max_cost=1000)(plan)
    assert exc.value.reject_code == "plan_relation_not_allowed"


def test_plan_check_refuses_expensive_plans(settings):
    settings.NL2SQL_MAX_COST = 100.0
    with pytest.raises(GuardError) as exc:
        services.make_plan_check(make_policy())(PLAN)
    assert exc.value.reject_code == "cost_exceeded"


def test_plan_check_refuses_an_uninspectable_plan():
    with pytest.raises(GuardError) as exc:
        services.make_plan_check(make_policy())([{}])
    assert exc.value.reject_code == "execution_error"


# --- execute pre-flight -------------------------------------------------------------------------------------
def _accepted():
    policy = make_policy()
    result = services.validate("SELECT region FROM sales", policy)
    assert result.ok
    return policy, result


def test_execute_refuses_rejected_results():
    policy = make_policy()
    bad = GuardResult(ok=False, reject_code="parse_error", message="", executed_sql=None, params={})
    with pytest.raises(GuardError) as exc:
        services.execute(bad, policy, QueryTarget(kind="warehouse", org_id=ORG))
    assert exc.value.reject_code == "execution_error"


def test_execute_refuses_external_targets_cleanly():
    policy, result = _accepted()
    with pytest.raises(GuardError) as exc:
        services.execute(result, policy, QueryTarget(kind="external", org_id=ORG, data_source_id=uuid.uuid4()))
    assert exc.value.reject_code == "execution_error"
    assert "PostgreSQL connector" in str(exc.value.detail)


def test_execute_refuses_a_target_of_another_org():
    policy, result = _accepted()
    with pytest.raises(GuardError) as exc:
        services.execute(result, policy, QueryTarget(kind="warehouse", org_id=uuid.uuid4()))
    assert exc.value.reject_code == "execution_error" and exc.value.status_code == 403


def test_execute_refuses_without_a_tenant_context():
    policy, result = _accepted()
    with pytest.raises(GuardError) as exc:
        services.execute(result, policy, QueryTarget(kind="warehouse", org_id=ORG))
    assert exc.value.status_code == 403


# --- metric expressions -------------------------------------------------------------------------------------
COLS = ["amount", "discount", "order_id", "region"]


@pytest.mark.parametrize(
    "expr, normalized",
    [
        ("SUM(amount)", "SUM(amount)"),
        ("sum(amount) - sum(discount)", "SUM(amount) - SUM(discount)"),
        ("SUM(amount) / NULLIF(COUNT(DISTINCT order_id), 0)", "SUM(amount) / NULLIF(COUNT(DISTINCT order_id), 0)"),
        ("count(*)", "COUNT(*)"),
        ("avg(amount) FILTER (WHERE region = 'North')", None),
    ],
)
def test_valid_metric_expressions(expr, normalized):
    out = services.validate_expression(expr, COLS)
    if normalized:
        assert out == normalized


@pytest.mark.parametrize(
    "expr, code",
    [
        ("", "parse_error"),
        ("amount", "invalid_sql"),
        ("SUM(amount); DROP TABLE x", "parse_error"),
        ("SUM(amount) -- x", "parse_error"),
        ("SUM(nope)", "unknown_column"),
        ("SUM(t.amount)", "column_not_allowed"),
        ("pg_sleep(1) + SUM(amount)", "forbidden_function"),
        ("(SELECT max(amount) FROM sales)", "invalid_sql"),
        ("SUM(amount) OVER ()", "invalid_sql"),
        ("SUM(amount::regclass)", "forbidden_cast"),
        ("SUM(amount) AS x, COUNT(*)", "invalid_sql"),
        ("x" * 2100, "too_large"),
    ],
)
def test_invalid_metric_expressions(expr, code):
    with pytest.raises(GuardReject) as exc:
        services.validate_expression(expr, COLS)
    assert exc.value.code == code, exc.value.message
