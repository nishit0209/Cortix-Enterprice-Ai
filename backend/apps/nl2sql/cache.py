"""Exact + semantic question cache with slot re-binding (AI_ARCHITECTURE.md §4).

* Exact: (org, scope, sha256(normalized question)). Relative periods ("last month") are re-bound to today.
* Semantic: local e5 embedding of the slot TEMPLATE ("top <num> region by revenue <period>"), cosine similarity
  >= 0.93 via apps.ai.vectors.nearest (pgvector SQL or numpy over the scope's rows), same slot shape; date/number
  slots re-bound to the new question's values. Skipped (exact cache + builder still work) when the local
  embedding model is unavailable; rows stored meanwhile get their vector from the `ai.reembed_pending` job.
* A hit is used only when (a) every slot whose value changed is templated in the SQL, (b) equality
  constraints still hold, and (c) the bound SQL only references tables/columns allowed to THIS user.
"""

from __future__ import annotations

import logging
import re
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
    if not literals_carried_over(ast, getattr(row, "normalized_question", "") or "", slots.normalized):
        return None  # "revenue in the North region" must never answer "... in the South region"
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


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def literals_carried_over(ast, cached_question: str, new_question: str) -> bool:
    """Every string literal of the cached SQL that came from the cached question's words (a filter value such as
    'North') must also occur in the new question. Literals that are not question words ('current_period') and
    dates (re-bound slots) are ignored."""
    from sqlglot import exp

    cached_q, new_q = f" {cached_question.lower()} ", f" {new_question.lower()} "
    for lit in ast.find_all(exp.Literal):
        if not lit.is_string:
            continue
        value = str(lit.this or "").strip().lower()
        if not value or _ISO_DATE_RE.match(value):
            continue
        pattern = re.compile(rf"(?<![\w]){re.escape(value)}(?![\w])")
        if pattern.search(cached_q) and not pattern.search(new_q):
            return False
    return True


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


SEMANTIC_MAX_SCAN = 5_000  # candidate rows ranked in numpy per scope + slot shape (array backend)


def _embed(text: str) -> list[float] | None:
    from apps.ai.services import EmbeddingUnavailable, embed_query, embedding_available

    if not embedding_available():
        return None
    try:
        return embed_query(text)
    except EmbeddingUnavailable as exc:
        log.info("nl2sql: local embedding model unavailable (%s); semantic cache skipped", exc.reason)
        return None
    except Exception:  # noqa: BLE001 - embeddings are an optimisation; never fail the question
        log.warning("nl2sql: local embedding failed; semantic cache skipped", exc_info=True)
        return None


def embed_template(text: str) -> list[float] | None:
    """Embedding used for the semantic cache (None when the local model is unavailable)."""
    return _embed(text)


def lookup_semantic(ctx: QuestionContext, slots: Slots, *, dataset_version_id: UUID | None = None,
                    threshold: float = SEMANTIC_THRESHOLD, embedding: list[float] | None = None) -> SqlProposal | None:
    from apps.ai.vectors import nearest

    from .models import BiQueryCache

    vec = embedding if embedding is not None else _embed(slots.template)
    if vec is None:
        return None
    try:
        with transaction.atomic():
            candidates = BiQueryCache.objects.filter(scope_id=scope_id(ctx, dataset_version_id),
                                                     slot_shape=slots.shape)
            rows = [r for r in nearest(candidates, "embedding", vec, 5, max_scan=SEMANTIC_MAX_SCAN)
                    if r.distance <= 1.0 - threshold + 1e-9]
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


# ------------------------------------------------------------------------------------------- re-embedding
REEMBED_SCAN_SQL = "SELECT DISTINCT org_id FROM nl2sql_biquerycache WHERE embedding IS NULL"


def reembed_missing(embedder, limit: int = 500) -> int:
    """Embed cache rows of the CURRENT tenant that were stored without a vector (model unavailable then).
    Raises EmbeddingUnavailable when the model is still missing."""
    from .models import BiQueryCache

    rows = list(BiQueryCache.objects.filter(embedding__isnull=True).order_by("created_at")
                .values_list("id", "template_question")[:max(1, int(limit))])
    done = 0
    for i in range(0, len(rows), 32):
        part = rows[i:i + 32]
        vectors = embedder.embed_queries([t for _, t in part])
        with transaction.atomic():
            for (row_id, _t), vec in zip(part, vectors, strict=True):
                BiQueryCache.objects.filter(id=row_id).update(embedding=[float(x) for x in vec])
        done += len(part)
    return done
