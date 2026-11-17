"""Cache re-binding logic (pure: rows are plain objects; DB behaviour lives in test_cache_db.py)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from apps.ai.embeddings import FakeEmbedder, cosine
from apps.nl2sql.builder import build_sql
from apps.nl2sql.cache import SEMANTIC_THRESHOLD, materialize
from apps.nl2sql.slots import extract_slots
from apps.nl2sql.sqlutil import templatize
from apps.nl2sql.tests.conftest import TODAY, make_ctx


def row_for(question: str, ctx, today=TODAY, sql: str | None = None):
    slots = extract_slots(question, today)
    sql = sql or build_sql(question, ctx, today=today).sql
    template, used, eq = templatize(sql, slots.values())
    return SimpleNamespace(question_text=question, slot_values=slots.values(), template_slots=used,
                           slot_equalities=eq, sql_template=template, explanation="cached explanation",
                           assumptions=["a"], chart_hint={"type": "bar", "x": "region", "y": ["total_amount"]})


def test_exact_hit_rebinds_relative_period_next_month():
    ctx = make_ctx()
    row = row_for("top 5 regions by revenue last month", ctx)
    later = date(2026, 12, 3)
    out = materialize(row, extract_slots("top 5 regions by revenue last month", later), ctx, "cache_exact")
    assert out is not None and out.source == "cache_exact"
    assert "'2026-11-01'" in out.sql and "'2026-12-01'" in out.sql and "LIMIT 5" in out.sql
    assert out.chart_hint.x == "region"


def test_same_values_keep_cached_explanation():
    ctx = make_ctx()
    row = row_for("top 5 regions by revenue last month", ctx)
    out = materialize(row, extract_slots("top 5 regions by revenue last month", TODAY), ctx, "cache_exact")
    assert out.explanation == "cached explanation"


def test_semantic_hit_rebinds_number_and_period():
    ctx = make_ctx()
    row = row_for("top 5 regions by revenue last month", ctx)
    out = materialize(row, extract_slots("top 3 regions by revenue in Q1 2026", TODAY), ctx, "cache_semantic")
    assert out is not None and out.source == "cache_semantic"
    assert "LIMIT 3" in out.sql and "'2026-01-01'" in out.sql and "'2026-04-01'" in out.sql
    assert "Re-used" in out.explanation


def test_untemplated_changed_slot_rejects_hit():
    ctx = make_ctx()
    # SQL whose time filter is not expressed with the question's literal dates
    sql = ('SELECT "region", SUM("amount") AS "t" FROM "sales" WHERE "order_date" >= CURRENT_DATE - 30 '
           'GROUP BY "region" LIMIT 5')
    row = row_for("top 5 regions by revenue last month", ctx, sql=sql)
    assert set(row.template_slots) == {"num_0"}
    same = materialize(row, extract_slots("top 5 regions by revenue last month", TODAY), ctx, "cache_exact")
    assert same is not None
    later = materialize(row, extract_slots("top 5 regions by revenue last month", date(2027, 1, 5)), ctx,
                        "cache_exact")
    assert later is None


def test_compare_template_respects_equality_constraint():
    ctx = make_ctx()
    row = row_for("compare revenue this month vs last month", ctx)
    assert row.slot_equalities  # this.start == last.end
    ok = materialize(row, extract_slots("compare revenue this week vs last week", TODAY), ctx, "cache_semantic")
    assert ok is not None and "'2026-11-16'" in ok.sql and "'2026-11-09'" in ok.sql
    bad = materialize(row, extract_slots("compare revenue in march 2026 vs in january 2026", TODAY), ctx,
                      "cache_semantic")
    assert bad is None


def test_hit_is_rechecked_against_the_current_users_policy():
    full = make_ctx()
    row = row_for("revenue by region", full)
    restricted = make_ctx(allowed={"region", "order_date", "quantity"})  # amount withheld for this user
    assert materialize(row, extract_slots("revenue by region", TODAY), restricted, "cache_exact") is None


def test_paraphrase_templates_are_close_unrelated_far():
    emb = FakeEmbedder()
    a = extract_slots("top 5 regions by revenue last month", TODAY).template
    b = extract_slots("top 10 regions by revenue last quarter", TODAY).template
    c = extract_slots("average discount by product category", TODAY).template
    va, vb, vc = emb.embed_queries([a, b, c])
    assert cosine(va, vb) >= SEMANTIC_THRESHOLD
    assert cosine(va, vc) < SEMANTIC_THRESHOLD
