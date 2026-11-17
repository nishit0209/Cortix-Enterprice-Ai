"""BiQueryCache with pgvector (DB + RLS). Uses FakeEmbedder (never downloads a model)."""

from __future__ import annotations

from datetime import date

import pytest

from apps.accounts.models import User
from apps.ai.embeddings import FakeEmbedder, override_embedder
from apps.ai.services import override_gemini
from apps.ai.testing import build_engine
from apps.core.tenancy import tenant_context
from apps.nl2sql import services
from apps.nl2sql.models import BiQueryCache
from apps.nl2sql.tests.conftest import TODAY, make_ctx
from apps.orgs.services import create_organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    owner = User.objects.create_user("cache.owner@example.test", "pw-Owner-123456")
    return create_organization(name="Cache Org", owner=owner)


@pytest.fixture
def env(org, settings):
    settings.GEMINI_API_KEY = ""
    engine, parts = build_engine()
    with override_embedder(FakeEmbedder()), override_gemini(engine), tenant_context(org.id):
        yield parts


SQL = ('SELECT "channel", SUM("amount") AS revenue FROM "sales" WHERE "order_date" >= \'2026-10-01\' '
       'AND "order_date" < \'2026-11-01\' GROUP BY "channel" ORDER BY revenue DESC LIMIT 5')
Q = "best 5 sales channels with discounts considered last month"


def test_remember_then_exact_hit_with_rebinding(env):
    ctx = make_ctx()
    row = services.remember(Q, SQL, ctx, source="llm", explanation="e", today=TODAY)
    assert row is not None and row.template_slots and row.embedding is not None
    hit = services.propose(Q, ctx, today=TODAY)
    assert hit.source == "cache_exact" and "LIMIT 5" in hit.sql
    later = services.propose(Q, ctx, today=date(2027, 2, 10))
    assert later.source == "cache_exact" and "'2027-01-01'" in later.sql and "'2027-02-01'" in later.sql
    assert BiQueryCache.objects.get().hit_count == 2
    assert env["transport"].total_calls == 0


def test_semantic_hit_rebinds_slots(env):
    ctx = make_ctx()
    services.store_verified(Q, SQL, ctx, today=TODAY)
    hit = services.propose("best 3 sales channels with discounts considered last quarter", ctx, today=TODAY)
    assert hit.source == "cache_semantic"
    assert "LIMIT 3" in hit.sql and "'2026-07-01'" in hit.sql and "'2026-10-01'" in hit.sql


def test_semantic_requires_same_slot_shape_and_similarity(env):
    ctx = make_ctx()
    services.store_verified(Q, SQL, ctx, today=TODAY)
    # unrelated question -> falls through to the builder / AI
    out = services.propose("average discount by product category", ctx, today=TODAY)
    assert out.source == "builder"


def test_verified_entry_not_overwritten_by_unverified(env):
    ctx = make_ctx()
    services.store_verified(Q, SQL, ctx, today=TODAY)
    services.remember(Q, SQL.replace("LIMIT 5", "LIMIT 6"), ctx, today=TODAY)
    row = BiQueryCache.objects.get()
    assert row.verified and row.source == "verified" and "LIMIT 6" not in row.sql_template


def test_store_refuses_sql_outside_policy(env):
    ctx = make_ctx()
    with pytest.raises(ValueError):
        services.store_verified("emails", 'SELECT "secret_col" FROM "sales"', ctx, today=TODAY)
    assert services.remember("x", "SELECT * FROM pg_user", ctx, today=TODAY) is None


def test_cache_hit_rechecked_for_restricted_user(env):
    full = make_ctx()
    services.store_verified(Q, SQL, full, today=TODAY)
    restricted = make_ctx(allowed={"order_date", "channel", "quantity"})
    out = services.propose(Q, restricted, today=TODAY)
    assert out.source != "cache_exact"


def test_cache_is_tenant_isolated(org, settings):
    other_owner = User.objects.create_user("cache.other@example.test", "pw-Owner-123456")
    other = create_organization(name="Other Cache Org", owner=other_owner)
    ctx = make_ctx()
    with override_embedder(FakeEmbedder()):
        with tenant_context(org.id):
            services.store_verified(Q, SQL, ctx, today=TODAY)
        with tenant_context(other.id):
            assert BiQueryCache.objects.count() == 0
