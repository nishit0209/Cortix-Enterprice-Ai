"""W3 semantic cache on PostgreSQL without / with the local embedding model (DB + RLS; FakeEmbedder only)."""

from __future__ import annotations

import pytest

from apps.accounts.models import User
from apps.ai.embeddings import FakeEmbedder, UnavailableEmbedder, override_embedder
from apps.ai.services import override_gemini
from apps.ai.testing import build_engine
from apps.core.tenancy import tenant_context
from apps.nl2sql import cache, services
from apps.nl2sql.models import BiQueryCache
from apps.nl2sql.tests.conftest import TODAY, make_ctx
from apps.orgs.services import create_organization

pytestmark = pytest.mark.django_db

SQL = ('SELECT "channel", SUM("amount") AS revenue FROM "sales" WHERE "order_date" >= \'2026-10-01\' '
       'AND "order_date" < \'2026-11-01\' GROUP BY "channel" ORDER BY revenue DESC LIMIT 5')
Q = "best 5 sales channels with discounts considered last month"
SIMILAR = "best 3 sales channels with discounts considered last quarter"


@pytest.fixture
def org():
    owner = User.objects.create_user("w3.cache.owner@example.test", "pw-Owner-123456")
    return create_organization(name="W3 Cache Org", owner=owner)


@pytest.fixture
def engine(settings):
    settings.GEMINI_API_KEY = ""
    eng, parts = build_engine()
    with override_gemini(eng):
        yield parts


def test_without_the_model_exact_cache_works_and_rows_wait_for_a_vector(org, engine):
    ctx = make_ctx()
    with override_embedder(UnavailableEmbedder()), tenant_context(org.id):
        row = services.store_verified(Q, SQL, ctx, today=TODAY)
        assert row is not None and row.embedding is None
        assert services.propose(Q, ctx, today=TODAY).source == "cache_exact"
        similar = services.propose(SIMILAR, ctx, today=TODAY)
        assert similar.source != "cache_semantic"  # semantic cache skipped, the request still answers
    assert engine["transport"].total_calls == 0

    with override_embedder(FakeEmbedder()), tenant_context(org.id):
        assert cache.reembed_missing(FakeEmbedder()) == 1
        assert BiQueryCache.objects.get().embedding is not None
        assert cache.reembed_missing(FakeEmbedder()) == 0
        hit = services.propose(SIMILAR, ctx, today=TODAY)
        assert hit.source == "cache_semantic" and "LIMIT 3" in hit.sql


def test_embedding_column_type_matches_the_vector_backend(org):
    from django.db import connection

    from apps.ai import vectors

    kind = vectors.column_type(connection, "nl2sql_biquerycache", "embedding")
    backend = vectors.field_backend(BiQueryCache._meta.get_field("embedding"), connection)
    assert backend == (vectors.PGVECTOR if kind.startswith("vector") else vectors.ARRAY)
    with connection.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'nl2sql_cache_emb_hnsw'")
        assert bool(cur.fetchone()) is (backend == vectors.PGVECTOR)
