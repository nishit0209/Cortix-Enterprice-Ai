"""propose() / repair() resolution order with a fake Gemini (network spy) and the cache stubbed out.

DB-backed cache behaviour is covered in test_cache_db.py.
"""

from __future__ import annotations

import pytest

from apps.ai.config import OrgAIConfig
from apps.ai.policy import check_policy
from apps.ai.services import override_gemini
from apps.ai.testing import FakeTransport, build_engine, unavailable
from apps.core.tenancy.context import current_org
from apps.nl2sql import cache, services
from apps.nl2sql.tests.conftest import DATASET, ORG, TODAY, make_ctx


@pytest.fixture(autouse=True)
def no_db_cache(monkeypatch):
    monkeypatch.setattr(cache, "lookup_exact", lambda *a, **k: None)
    monkeypatch.setattr(cache, "lookup_semantic", lambda *a, **k: None)


@pytest.fixture
def tenant():
    token = current_org.set(ORG)
    yield
    current_org.reset(token)


@pytest.fixture
def key(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = "gemini-3.5-flash-lite"
    settings.GEMINI_MODEL_REASONING = "gemini-3.8-flash"


def llm(sql, **extra):
    return FakeTransport.json({"sql": sql, "explanation": "Revenue per channel and region.", "assumptions": ["net"],
                               "chart_hint": {"type": "bar", "x": "channel", "y": ["revenue"], "series": "region"},
                               "clarification": None, **extra})


def gate(policy: str, classification: str, consent: bool = True):
    def _check(payload):
        return check_policy(payload, state={"policy": policy, "consent_valid": consent},
                            classify=lambda ref: classification, tier="free")

    return _check


NOVEL = "revenue by channel and region for orders with a discount"


def test_builder_answers_without_gemini(tenant, key):
    engine, p = build_engine()
    with override_gemini(engine):
        out = services.propose("top 5 regions by revenue last month", make_ctx(), today=TODAY)
    assert out.source == "builder" and "LIMIT 5" in out.sql
    assert p["transport"].total_calls == 0


def test_cache_hit_short_circuits(monkeypatch, tenant, key):
    from nexora.contracts import SqlProposal

    hit = SqlProposal(sql="SELECT 1", source="cache_exact")
    monkeypatch.setattr(cache, "lookup_exact", lambda *a, **k: hit)
    engine, p = build_engine()
    with override_gemini(engine):
        assert services.propose("anything at all", make_ctx(), today=TODAY) is hit
    assert p["transport"].total_calls == 0


def test_llm_path_one_call_with_schema_only_prompt(tenant, key):
    engine, p = build_engine(policy_check=gate("schema_only", "internal"))
    p["transport"].push(llm('SELECT "channel", "region", SUM("amount") AS revenue FROM "sales" '
                            'WHERE "discount" > 0 GROUP BY 1, 2'))
    ctx = make_ctx(history=({"question": "revenue for asha.k@example.test", "columns": ["customer_email", "amount"],
                             "sql": "SELECT customer_email, SUM(amount) FROM sales "
                                    "WHERE customer_email = 'asha.k@example.test' GROUP BY 1"},))
    with override_gemini(engine):
        out = services.propose(NOVEL, ctx, today=TODAY)
    assert out.source == "llm"
    assert out.sql.endswith("LIMIT 1000")  # LIMIT enforced locally
    assert out.chart_hint.type == "bar" and out.chart_hint.series == "region"
    assert len(out.ai_request_ids) == 1
    [req] = p["transport"].calls
    prompt = req.prompt
    # SCHEMA only: column names/types/descriptions, metrics, question, value-free history
    assert '"amount" numeric(12,2)' in prompt and "Net invoice amount in INR" in prompt
    assert "SUM(amount) / NULLIF(COUNT(DISTINCT order_id), 0)" in prompt
    assert NOVEL in prompt and "2026-11-17" in prompt
    # PII column aliased; its name and the history value never leave
    assert "customer_email" not in prompt and "pii_col_1" in prompt
    assert "example.test" not in prompt and "[EMAIL]" not in prompt.split("conversation_summary")[0]
    assert "Synthetic Sales" in prompt
    assert req.model == "gemini-3.5-flash-lite"
    rec = p["recorder"].records[0]
    assert rec.payload_classes == ["SCHEMA"] and rec.payload_assets == [f"dataset:{DATASET}"]


def test_pii_alias_in_model_sql_is_mapped_back(tenant, key):
    engine, p = build_engine()
    p["transport"].push(llm('SELECT "pii_col_1", COUNT(*) AS n FROM "sales" GROUP BY 1 LIMIT 10'))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert '"customer_email"' in out.sql and "pii_col_1" not in out.sql
    assert out.sql.endswith("LIMIT 10")


def test_synthetic_only_real_dataset_zero_network_calls(tenant, key):
    """AC: a new org (synthetic_only) asking about a real dataset makes ZERO Gemini calls."""
    engine, p = build_engine(policy_check=gate("synthetic_only", "internal"))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.source == "none" and out.sql is None
    assert out.blocked_reason == "ai_policy_blocked"
    assert "visual query builder" in out.explanation
    assert p["transport"].total_calls == 0
    assert p["recorder"].statuses == ["blocked_by_policy"]
    assert len(out.ai_request_ids) == 1


def test_synthetic_only_still_answers_builder_questions(tenant, key):
    engine, p = build_engine(policy_check=gate("synthetic_only", "internal"))
    with override_gemini(engine):
        out = services.propose("revenue by region last month", make_ctx(), today=TODAY)
    assert out.source == "builder" and p["transport"].total_calls == 0


def test_synthetic_dataset_allowed_under_synthetic_only(tenant, key):
    engine, p = build_engine(policy_check=gate("synthetic_only", "synthetic"))
    p["transport"].push(llm('SELECT "channel", SUM("amount") AS revenue FROM "sales" GROUP BY 1'))
    with override_gemini(engine):
        assert services.propose(NOVEL, make_ctx(), today=TODAY).source == "llm"


def test_revoked_consent_blocks_next_request(tenant, key):
    engine, p = build_engine(policy_check=gate("schema_only", "internal", consent=False))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.blocked_reason == "ai_policy_blocked" and p["transport"].total_calls == 0


@pytest.mark.parametrize(("config", "reason"), [
    (OrgAIConfig(daily_request_budget=0), "ai_quota_exhausted"),
])
def test_quota_exhausted_returns_none(tenant, key, config, reason):
    engine, p = build_engine(org_config=config)
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.source == "none" and out.blocked_reason == reason and p["transport"].total_calls == 0


def test_not_configured_returns_none(tenant, settings):
    settings.GEMINI_API_KEY = ""
    engine, p = build_engine()
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.blocked_reason == "ai_not_configured" and p["transport"].total_calls == 0


def test_unavailable_returns_none(tenant, key):
    engine, p = build_engine()
    p["transport"].push(unavailable(), unavailable(), unavailable())
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.blocked_reason == "ai_unavailable"


def test_clarification_returned_without_sql(tenant, key):
    engine, p = build_engine()
    p["transport"].push(llm(None, clarification="Which revenue: gross or net?"))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.sql is None and out.source == "llm" and out.clarification == "Which revenue: gross or net?"


def test_unknown_column_gets_exactly_one_repair_on_reasoning_tier(tenant, key):
    engine, p = build_engine()
    p["transport"].push(llm('SELECT "chanel", SUM("amount") FROM "sales" GROUP BY 1'),
                        llm('SELECT "channel", SUM("amount") AS revenue FROM "sales" GROUP BY 1'))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.source == "llm" and '"channel"' in out.sql
    assert [c.model for c in p["transport"].calls] == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]
    repair_prompt = p["transport"].calls[1].prompt
    assert "column_not_allowed" in repair_prompt and '"chanel"' in repair_prompt
    assert len(out.ai_request_ids) == 2


def test_repair_failure_gives_up_after_one_attempt(tenant, key):
    engine, p = build_engine()
    p["transport"].push(llm('SELECT "nope" FROM "sales"'), llm('SELECT "still_nope" FROM "sales"'))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.sql is None and out.blocked_reason == "ai_invalid_output"
    assert len(p["transport"].calls) == 2


@pytest.mark.parametrize("sql", [
    "SELECT * FROM pg_catalog.pg_user",
    "SELECT 1; DROP TABLE sales",
    'SELECT ctid FROM "sales"',
])
def test_security_problems_are_never_repaired(tenant, key, sql):
    engine, p = build_engine()
    p["transport"].push(llm(sql))
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.sql is None and out.blocked_reason == "ai_invalid_output"
    assert len(p["transport"].calls) == 1


def test_multi_table_uses_reasoning_tier(tenant, key):
    meta = {"sales": ({"name": "store_id", "type": "int"}, {"name": "amount", "type": "numeric"}),
            "stores": ({"name": "store_id", "type": "int"}, {"name": "city", "type": "text"})}
    ctx = make_ctx(tables={"sales": "s.a", "stores": "s.b"}, columns_meta=meta)
    engine, p = build_engine()
    p["transport"].push(llm('SELECT s2."city", SUM(s1."amount") AS revenue FROM "sales" s1 JOIN "stores" s2 '
                            'ON s1."store_id" = s2."store_id" GROUP BY 1'))
    with override_gemini(engine):
        out = services.propose("revenue by city", ctx, today=TODAY)
    assert out.source == "llm"
    assert p["transport"].calls[0].model == "gemini-3.8-flash"


def test_public_repair_refuses_security_codes(tenant, key):
    engine, p = build_engine()
    with override_gemini(engine):
        for code in ("forbidden_function", "system_catalog", "multiple_statements", "whole_row_reference",
                     "something_unknown"):
            out = services.repair(NOVEL, "SELECT pg_sleep(10)", code, make_ctx(), today=TODAY)
            assert out.sql is None and out.blocked_reason == "not_repairable"
    assert p["transport"].total_calls == 0


def test_public_repair_aliases_pii_before_sending(tenant, key):
    engine, p = build_engine()
    p["transport"].push(llm('SELECT "pii_col_1", SUM("amount") AS revenue FROM "sales" GROUP BY 1'))
    with override_gemini(engine):
        out = services.repair(NOVEL, 'SELECT "customer_email", SUM("amount") FROM "sales" GROUP BY 1 HAVING x > 1',
                              "column_not_allowed", make_ctx(), reject_message="unknown column x", today=TODAY)
    assert out.source == "llm" and '"customer_email"' in out.sql
    sent = p["transport"].calls[0].prompt
    assert "customer_email" not in sent and "pii_col_1" in sent


def test_empty_question():
    out = services.propose("   ", make_ctx(), today=TODAY)
    assert out.source == "none" and out.clarification
