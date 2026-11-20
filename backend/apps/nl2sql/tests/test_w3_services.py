"""W3 nl2sql services: quota blocks carry resets_at, profiled dimension values, the semantic cache without the
embedding model, literal safety of cache hits. No database (lookups are stubbed), no Gemini (FakeTransport)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlglot

from apps.ai.config import OrgAIConfig
from apps.ai.embeddings import FakeEmbedder, UnavailableEmbedder
from apps.ai.policy import check_policy
from apps.ai.services import override_embedder, override_gemini
from apps.ai.testing import FakeTransport, build_engine
from apps.core.tenancy.context import current_org
from apps.nl2sql import cache, services, values
from apps.nl2sql.tests.conftest import ORG, TODAY, make_ctx

NOVEL = "revenue by channel and region for orders with a discount"


@pytest.fixture(autouse=True)
def no_db_cache(monkeypatch):
    monkeypatch.setattr(cache, "lookup_exact", lambda *a, **k: None)
    monkeypatch.setattr(cache, "lookup_semantic", lambda *a, **k: None)
    values.clear_cache()
    yield
    values.clear_cache()


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


def synthetic_gate(payload):
    return check_policy(payload, state={"policy": "synthetic_only", "consent_valid": False},
                        classify=lambda ref: "synthetic", tier="free")


def llm_ok():
    return FakeTransport.json({"sql": "SELECT channel, region, SUM(amount) AS revenue FROM sales "
                                      "WHERE discount > 0 GROUP BY channel, region",
                               "explanation": "Revenue by channel and region.", "assumptions": [],
                               "chart_hint": None, "clarification": None})


# ------------------------------------------------------------------------------------- quota blocks
def test_quota_block_carries_resets_at_for_bi(tenant, key):
    engine, parts = build_engine(transport=FakeTransport([llm_ok()]), policy_check=synthetic_gate,
                                 org_config=OrgAIConfig(daily_request_budget=1))
    with override_gemini(engine):
        first = services.propose(NOVEL, make_ctx(), today=TODAY)
        second = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert first.source == "llm" and first.sql
    assert second.source == "none" and second.blocked_reason == "ai_quota_exhausted"
    assert isinstance(second, services.BlockedProposal)
    resets = datetime.fromisoformat(second.resets_at)
    assert resets > datetime.fromtimestamp(parts["clock"](), tz=UTC)
    assert second.quota_scope == "org_day" and second.retry_after_s > 0
    assert services.blocked_details(second) == {"resets_at": second.resets_at,
                                                "retry_after_s": second.retry_after_s}
    assert parts["transport"].total_calls == 1  # the refusal happens before any network call


def test_policy_block_has_no_quota_fields(tenant, key):
    def blocked(payload):
        return check_policy(payload, state={"policy": "synthetic_only", "consent_valid": False},
                            classify=lambda ref: "internal", tier="free")

    engine, parts = build_engine(transport=FakeTransport([llm_ok()]), policy_check=blocked)
    with override_gemini(engine):
        out = services.propose(NOVEL, make_ctx(), today=TODAY)
    assert out.blocked_reason == "ai_policy_blocked" and out.resets_at is None
    assert services.blocked_details(out) == {}
    assert parts["transport"].total_calls == 0


def test_blocked_details_of_a_plain_proposal():
    assert services.blocked_details(services.SqlProposal(sql="SELECT 1", source="builder")) == {}


# ------------------------------------------------------------------------------------- profiled values
COLUMNS = [
    {"name": "region", "is_pii": False, "stats": {"top_values": [{"value": "North", "count": 9},
                                                                {"value": "South", "count": 3},
                                                                {"value": "North", "count": 1}]}},
    {"name": "customer_email", "is_pii": True, "stats": {"top_values": None}},
    {"name": "quantity", "is_pii": False, "stats": {"top_values": [{"value": 3, "count": 9}]}},
    {"name": "channel", "is_pii": False, "stats": {}},
]


def test_profile_values_keeps_non_pii_strings_once():
    assert values.profile_values(COLUMNS) == {"region": ("North", "South")}


def test_apply_values_skips_masked_pii_and_listed_columns():
    ctx = make_ctx()
    meta = tuple({**m, "masked": True} if m["name"] == "channel" else m for m in ctx.columns_meta["sales"])
    ctx = replace(ctx, columns_meta={"sales": meta})
    out = values.apply_values(ctx, {"region": ("North",), "channel": ("Online",), "customer_email": ("x@y.z",)})
    by_name = {m["name"]: m for m in out.columns_meta["sales"]}
    assert by_name["region"]["values"] == ["North"]
    assert "values" not in by_name["channel"] and "values" not in by_name["customer_email"]
    again = values.apply_values(out, {"region": ("Other",)})  # already listed: untouched
    assert {m["name"]: m for m in again.columns_meta["sales"]}["region"]["values"] == ["North"]
    assert values.apply_values(ctx, {}) is ctx


def test_known_values_are_cached_per_version(monkeypatch, tenant):
    from apps.datasets import services as datasets

    version_id = uuid4()
    calls = []

    class Version:
        id = version_id
        status = "ready"

    monkeypatch.setattr(datasets, "get_dataset", lambda ds_id: calls.append(ds_id) or object())
    monkeypatch.setattr(datasets, "current_version", lambda dataset: Version())
    monkeypatch.setattr(datasets, "version_columns", lambda version: COLUMNS)
    ctx = make_ctx()
    assert values.known_values(ctx, version_id) == {"region": ("North", "South")}
    assert values.known_values(ctx, version_id) == {"region": ("North", "South")}
    assert len(calls) == 1  # second call served from the per-version cache
    assert values.known_values(ctx, uuid4()) == {}  # another version: never guess


def test_with_known_values_never_touches_the_db_outside_the_tenant(monkeypatch):
    monkeypatch.setattr(values, "known_values", lambda *a, **k: pytest.fail("must not look up values"))
    ctx = make_ctx()
    assert values.with_known_values(ctx) is ctx


def test_with_known_values_degrades_on_errors(monkeypatch, tenant):
    def boom(*a, **k):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(values, "known_values", boom)
    ctx = make_ctx()
    assert values.with_known_values(ctx) is ctx


def test_propose_uses_profiled_values_for_the_builder(monkeypatch, tenant, key):
    monkeypatch.setattr(values, "known_values", lambda ctx, version: {"product_category": ("Electronics", "Home")})
    engine, parts = build_engine(transport=FakeTransport([llm_ok()]), policy_check=synthetic_gate)
    with override_gemini(engine):
        out = services.propose("total sales from electronics last month", make_ctx(), today=TODAY)
    assert out.source == "builder"
    assert "\"product_category\" = 'Electronics'" in out.sql
    assert parts["transport"].total_calls == 0


def test_schema_card_never_carries_values():
    from apps.nl2sql.prompting import build_schema_card

    ctx = values.apply_values(make_ctx(), {"region": ("North", "South")})
    card = build_schema_card(ctx)
    assert "North" not in repr(card.tables)


# ------------------------------------------------------------------------------------- semantic cache
def test_semantic_cache_is_skipped_without_the_embedding_model(monkeypatch):
    monkeypatch.undo()  # the real lookup_semantic, but no DB is reached: the embedding step stops first
    from apps.nl2sql.slots import extract_slots

    with override_embedder(UnavailableEmbedder()) as emb:
        assert cache._embed("top <num> region by revenue") is None
        assert emb.calls == 0  # availability is checked before any embedding attempt
        slots = extract_slots("top 5 regions by revenue", TODAY)
        assert cache.lookup_semantic(make_ctx(), slots) is None


def test_semantic_embedding_errors_degrade(monkeypatch):
    class Broken(FakeEmbedder):
        def embed_queries(self, texts):
            raise MemoryError("out of memory")

    with override_embedder(Broken(dim=8)):
        assert cache._embed("x") is None
    with override_embedder(FakeEmbedder(dim=8)):
        assert len(cache._embed("revenue by region")) == 8


@pytest.mark.parametrize(("cached", "new", "ok"), [
    ("revenue in the north region", "revenue in the north region last month", True),
    ("revenue in the north region", "revenue in the south region", False),
    ("revenue by region", "sales by region", True),
])
def test_cache_hits_never_swap_filter_values(cached, new, ok):
    ast = sqlglot.parse_one("SELECT SUM(amount) FROM sales WHERE LOWER(region) = 'north' "
                            "AND order_date >= DATE '2026-10-01'" if "north" in cached
                            else "SELECT region, SUM(amount) FROM sales GROUP BY region", read="postgres")
    assert cache.literals_carried_over(ast, cached, new) is ok
