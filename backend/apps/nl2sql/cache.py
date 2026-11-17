"""Exact + semantic question cache with slot re-binding (AI_ARCHITECTURE.md §4).

* Exact: (org, scope, sha256(normalized question)). Relative periods ("last month") are re-bound to today.
* Semantic: local e5 embedding of the slot TEMPLATE ("top <num> region by revenue <period>"), pgvector cosine
  >= 0.93, same slot shape; date/number slots re-bound to the new question's values.
* A hit is used only when (a) every slot whose value changed is templated in the SQL, (b) equality
  constraints still hold, and (c) the bound SQL only references tables/columns allowed to THIS user.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import DatabaseError, transaction
from django.db.models import F
from django.utils import timezone

from nexora.contracts import ChartHint, QuestionContext, SqlProposal

from .slots import Slots
from .sqlutil import bind as bind_template
from .sqlutil import parse_select, render, templatize, validate_against_context

log = logging.getLogger("nexora.nl2sql")

SEMANTIC_THRESHOLD = 0.93


def scope_id(ctx: QuestionContext, dataset_version_id: UUID | None = None) -> UUID:
    return dataset_version_id or ctx.dataset_id


def _allowed(ctx: QuestionContext) -> dict:
    return {t: ctx.policy.allowed_columns.get(t, frozenset()) for t in ctx.policy.tables}


def chart_to_json(hint: ChartHint | None) -> dict | None:
    if hint is None:
        return None
    return {"type": hint.type, "x": hint.x, "y": list(hint.y), "series": hint.series}


def chart_from_json(data: dict | None) -> ChartHint | None:
    if not data or data.get("type") not in ("bar", "line", "area", "pie", "scatter", "table", "kpi"):
        return None
    return ChartHint(type=data["type"], x=data.get("x"), y=tuple(data.get("y") or ()), series=data.get("series"))


def materialize(row, slots: Slots, ctx: QuestionContext, source: str) -> SqlProposal | None:
    """Re-bind a cached template to the new question's slots and re-check it against the user's policy."""
    new_values = slots.values()
    cached_values = dict(row.slot_values or {})
    templated = set(row.template_slots or [])
    for name, value in new_values.items():
        if name not in templated and value != cached_values.get(name):
            return None  # the SQL hard-codes a value that differs for this question
    sql = bind_template(row.sql_template, new_values, row.slot_equalities or [])
    if sql is None:
        return None
    ast, issue = parse_select(sql)
    if issue:
        return None
    if validate_against_context(ast, ctx.policy.tables, _allowed(ctx)) is not None:
        return None
    changed = any(new_values.get(n) != cached_values.get(n) for n in templated)
    explanation = row.explanation or None
    if changed:
        explanation = f"Re-used the answer to a similar question (“{row.question_text}”) with your values."
    return SqlProposal(
        sql=render(ast),
        source=source,  # type: ignore[arg-type]
        explanation=explanation,
        assumptions=tuple(row.assumptions or ()),
        chart_hint=chart_from_json(row.chart_hint),
    )


def _touch(row) -> None:
    from .models import BiQueryCache

    try:
        with transaction.atomic():
            BiQueryCache.objects.filter(id=row.id).update(hit_count=F("hit_count") + 1, last_hit_at=timezone.now())
    except DatabaseError:
        log.warning("nl2sql: cache hit counter update failed", exc_info=True)


def lookup_exact(ctx: QuestionContext, slots: Slots, *, dataset_version_id: UUID | None = None) -> SqlProposal | None:
    from .models import BiQueryCache

    try:
        with transaction.atomic():
            row = BiQueryCache.objects.filter(scope_id=scope_id(ctx, dataset_version_id),
                                              question_hash=slots.hash).first()
    except DatabaseError:
        log.warning("nl2sql: exact cache lookup failed", exc_info=True)
        return None
    if row is None:
        return None
    proposal = materialize(row, slots, ctx, "cache_exact")
    if proposal is not None:
        _touch(row)
    return proposal


def _embed(text: str) -> list[float] | None:
    from apps.ai.services import embed_query

    try:
        return embed_query(text)
    except Exception:  # noqa: BLE001 - embeddings are an optimisation; never fail the question
        log.warning("nl2sql: local embedding unavailable; semantic cache skipped", exc_info=True)
        return None


def lookup_semantic(ctx: QuestionContext, slots: Slots, *, dataset_version_id: UUID | None = None,
                    threshold: float = SEMANTIC_THRESHOLD, embedding: list[float] | None = None) -> SqlProposal | None:
    from pgvector.django import CosineDistance

    from .models import BiQueryCache

    vec = embedding if embedding is not None else _embed(slots.template)
    if vec is None:
        return None
    try:
        with transaction.atomic():
            rows = list(
                BiQueryCache.objects.filter(scope_id=scope_id(ctx, dataset_version_id), slot_shape=slots.shape,
                                            embedding__isnull=False)
                .annotate(distance=CosineDistance("embedding", vec))
                .filter(distance__lte=1.0 - threshold)
                .order_by("distance")[:5]
            )
    except DatabaseError:
        log.warning("nl2sql: semantic cache lookup failed", exc_info=True)
        return None
    for row in rows:
        proposal = materialize(row, slots, ctx, "cache_semantic")
        if proposal is not None:
            _touch(row)
            return proposal
    return None


def store(question: str, sql: str, ctx: QuestionContext, *, slots: Slots, source: str, verified: bool = False,
          explanation: str | None = None, assumptions: tuple[str, ...] = (), chart_hint: ChartHint | None = None,
          user_id: UUID | None = None, dataset_version_id: UUID | None = None,
          embedding: list[float] | None = None):
    """Upsert a cache entry (templated pre-policy SQL). A verified entry is never replaced by an unverified one."""
    from sqlglot import exp

    from .models import BiQueryCache

    ast, issue = parse_select(sql)
    if issue:
        raise ValueError(f"cannot cache invalid SQL: {issue.code}")
    problem = validate_against_context(ast, ctx.policy.tables, _allowed(ctx))
    if problem:
        raise ValueError(f"cannot cache SQL outside the policy: {problem.code}")
    canonical_sql = render(ast)
    template, used, equalities = templatize(canonical_sql, slots.values())
    tables = sorted({t.name for t in ast.find_all(exp.Table)})
    columns = sorted({c.name for c in ast.find_all(exp.Column) if c.name})
    vec = embedding if embedding is not None else _embed(slots.template)
    scope = scope_id(ctx, dataset_version_id)
    defaults = {
        "dataset_id": ctx.dataset_id,
        "dataset_version_id": dataset_version_id,
        "question_text": question.strip()[:2000],
        "normalized_question": slots.normalized,
        "template_question": slots.template,
        "slot_shape": slots.shape,
        "slot_values": slots.values(),
        "sql_template": template,
        "template_slots": used,
        "slot_equalities": equalities,
        "tables": tables,
        "columns": columns,
        "explanation": explanation or "",
        "assumptions": list(assumptions),
        "chart_hint": chart_to_json(chart_hint),
        "source": "verified" if verified else source,
        "verified": verified,
        "embedding": vec,
    }
    with transaction.atomic():
        existing = BiQueryCache.objects.select_for_update().filter(scope_id=scope, question_hash=slots.hash).first()
        if existing is not None:
            if existing.verified and not verified:
                return existing
            for k, v in defaults.items():
                setattr(existing, k, v)
            if verified:
                existing.verified_by = user_id
            existing.save()
            return existing
        return BiQueryCache.objects.create(
            scope_id=scope, question_hash=slots.hash, created_by=user_id,
            verified_by=user_id if verified else None, **defaults,
        )
