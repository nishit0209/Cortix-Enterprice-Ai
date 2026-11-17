"""Public service API of apps.nl2sql. Other apps import only from here.

    propose(question, ctx)                        -> SqlProposal  (contract: nexora.contracts.NL2SQL)
    repair(question, bad_sql, reject_code, ctx)   -> SqlProposal  (one repair after a NON-security rejection)
    store_verified(question, sql, ctx)            -> promote a thumbs-up answer into the cache
    remember(question, sql, ctx, source=...)      -> cache a guard-approved, executed answer (unverified)

Resolution order: exact cache -> semantic cache -> deterministic builder -> Gemini (`nl2sql.generate`, one
call returning sql + explanation + assumptions + chart_hint + clarification) -> at most one repair.
The Gemini payload is SCHEMA on dataset:<id>; the policy gate inside apps.ai decides before any network call.
"""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from django.utils import timezone

from apps.ai.exceptions import AIInvalidOutput, AIPolicyBlocked, AIQuotaExhausted, AIUnavailable
from apps.ai.services import gemini, get_prompt, request_trace
from nexora.contracts import ChartHint, PayloadClass, PayloadManifest, QuestionContext, SqlProposal

from . import cache
from .builder import build_sql
from .prompting import build_schema_card, history_summary, metric_defs
from .slots import extract_slots
from .sqlutil import (
    MAX_LIMIT,
    REPAIRABLE_CODES,
    SECURITY_CODES,
    enforce_limit,
    parse_select,
    rename_identifiers,
    render,
    validate_against_context,
)

log = logging.getLogger("nexora.nl2sql")

BLOCKED_MESSAGES = {
    "ai_policy_blocked": ("Your organization's AI data policy does not allow describing this dataset to Gemini. "
                          "Try rephrasing as a simple question (e.g. 'revenue by region last month') or use "
                          "the visual query builder."),
    "ai_quota_exhausted": "The AI quota is used up for now. Simple questions and saved queries still work.",
    "ai_rate_limited": "AI is busy right now. Please retry in a minute.",
    "ai_not_configured": "AI is not configured on this server. Simple questions and the visual builder still work.",
    "ai_unavailable": "AI is temporarily unavailable. Please retry.",
    "ai_limiter_unavailable": "AI is temporarily unavailable. Please retry.",
    "ai_invalid_output": "Couldn't produce a valid query for that question. You can edit the SQL manually.",
}


def _today(today: date | None) -> date:
    return today or timezone.localdate()


def _payload(ctx: QuestionContext) -> PayloadManifest:
    return PayloadManifest({PayloadClass.SCHEMA: (f"dataset:{ctx.dataset_id}",)})


def _none(reason: str, ids=(), explanation: str | None = None) -> SqlProposal:
    return SqlProposal(sql=None, source="none", explanation=explanation or BLOCKED_MESSAGES.get(reason),
                       blocked_reason=reason, ai_request_ids=tuple(ids))


def _allowed(ctx: QuestionContext) -> dict:
    return {t: ctx.policy.allowed_columns.get(t, frozenset()) for t in ctx.policy.tables}


def _chart(data: dict | None, sql_ast) -> ChartHint | None:
    if not isinstance(data, dict):
        return None
    typ = data.get("type")
    if typ not in ("bar", "line", "area", "pie", "scatter", "table", "kpi"):
        return None
    y = data.get("y") or []
    if isinstance(y, str):
        y = [y]
    return ChartHint(type=typ, x=data.get("x") or None, y=tuple(str(v) for v in y if v),
                     series=data.get("series") or None)


def _llm_call(prompt_key: str, ctx: QuestionContext, *, tier: str, **render_kwargs) -> dict:
    spec = get_prompt(prompt_key)
    prompt = spec.render(**render_kwargs)
    return gemini().generate_structured(
        feature="nl2sql", prompt_key=spec.key, system=spec.system, prompt=prompt,
        schema=spec.response_schema, payload=_payload(ctx), tier=tier, user_id=ctx.user_id,
    )


def _render_inputs(question: str, ctx: QuestionContext, today: date):
    card = build_schema_card(ctx)
    return card, {
        "dataset_name": ctx.dataset_name,
        "tables": card.tables,
        "metrics": metric_defs(ctx, card),
        "history": history_summary(ctx, card),
        "question": question,
        "today": today.isoformat(),
        "row_limit": min(MAX_LIMIT, int(ctx.policy.row_cap or MAX_LIMIT)),
    }


def _postprocess(data: dict, ctx: QuestionContext, card) -> tuple[SqlProposal | None, str | None, str, str]:
    """Return (proposal, issue_code, issue_message, raw_sql_with_aliases)."""
    sql = (data.get("sql") or "").strip() if isinstance(data.get("sql"), str) else ""
    clarification = (data.get("clarification") or "").strip() if isinstance(data.get("clarification"), str) else ""
    explanation = str(data.get("explanation") or "") or None
    assumptions = tuple(str(a) for a in (data.get("assumptions") or []) if a)
    if not sql:
        return SqlProposal(
            sql=None, source="llm", explanation=explanation, assumptions=assumptions,
            clarification=clarification or "Could you rephrase the question with the measure and grouping you need?",
        ), None, "", ""
    ast, issue = parse_select(sql)
    if issue:
        return None, issue.code, issue.message, sql
    ast = rename_identifiers(ast, card.alias_to_real)  # PII aliases -> real column names (locally)
    problem = validate_against_context(ast, ctx.policy.tables, _allowed(ctx))
    if problem:
        return None, problem.code, problem.message, sql
    ast = enforce_limit(ast, min(MAX_LIMIT, int(ctx.policy.row_cap or MAX_LIMIT)))
    chart = _chart(data.get("chart_hint"), ast)
    if chart is not None:
        rev = {**card.alias_to_real}
        chart = ChartHint(type=chart.type, x=rev.get(chart.x or "", chart.x), y=tuple(rev.get(v, v) for v in chart.y),
                          series=rev.get(chart.series or "", chart.series))
    return SqlProposal(
        sql=render(ast), source="llm", explanation=explanation, assumptions=assumptions, chart_hint=chart,
        clarification=clarification or None,
    ), None, "", sql


def _ai_failure(exc: Exception) -> str:
    code = None
    if hasattr(exc, "get_codes"):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else None
    if isinstance(exc, AIPolicyBlocked):
        return "ai_policy_blocked"
    if isinstance(exc, AIInvalidOutput):
        return "ai_invalid_output"
    if isinstance(exc, AIQuotaExhausted):
        return code or "ai_quota_exhausted"
    return code or "ai_unavailable"


def propose(question: str, ctx: QuestionContext, *, today: date | None = None,
            dataset_version_id: UUID | None = None) -> SqlProposal:
    """NL2SQL.propose — never raises for AI unavailability; returns source='none' + blocked_reason instead."""
    question = (question or "").strip()
    if not question:
        return SqlProposal(sql=None, source="none", clarification="Please type a question about this dataset.")
    today = _today(today)
    slots = extract_slots(question, today)

    hit = cache.lookup_exact(ctx, slots, dataset_version_id=dataset_version_id)
    if hit is not None:
        return hit
    hit = cache.lookup_semantic(ctx, slots, dataset_version_id=dataset_version_id)
    if hit is not None:
        return hit

    built = build_sql(question, ctx, today=today)
    if built is not None:
        return SqlProposal(sql=built.sql, source="builder", explanation=built.explanation,
                           assumptions=built.assumptions, chart_hint=built.chart_hint)

    return _generate(question, ctx, today)


def _generate(question: str, ctx: QuestionContext, today: date) -> SqlProposal:
    card, inputs = _render_inputs(question, ctx, today)
    tier = "reasoning" if len(ctx.policy.tables) > 1 else "fast"
    with request_trace() as ids:
        try:
            data = _llm_call("nl2sql.generate", ctx, tier=tier, **inputs)
        except (AIPolicyBlocked, AIQuotaExhausted, AIUnavailable, AIInvalidOutput) as exc:
            return _none(_ai_failure(exc), ids)
        proposal, code, message, raw_sql = _postprocess(data, ctx, card)
        if proposal is not None:
            return _with_ids(proposal, ids)
        if code not in REPAIRABLE_CODES:
            log.warning("nl2sql: model SQL rejected locally with non-repairable code %s", code)
            return _none("ai_invalid_output", ids,
                         explanation=f"The generated query was rejected ({code}) and was not retried.")
        repaired = _repair_call(question, raw_sql, code, message, ctx, today, card, inputs)
        return _with_ids(repaired, ids)


def _with_ids(p: SqlProposal, ids) -> SqlProposal:
    return SqlProposal(sql=p.sql, source=p.source, explanation=p.explanation, assumptions=p.assumptions,
                       chart_hint=p.chart_hint, clarification=p.clarification,
                       ai_request_ids=tuple(p.ai_request_ids) + tuple(i for i in ids if i not in p.ai_request_ids),
                       blocked_reason=p.blocked_reason)


def _repair_call(question, bad_sql_aliased, code, message, ctx, today, card, inputs) -> SqlProposal:
    repair_inputs = {k: v for k, v in inputs.items() if k != "history"}
    repair_inputs["history"] = inputs.get("history", [])
    try:
        data = _llm_call("nl2sql.repair", ctx, tier="reasoning", bad_sql=bad_sql_aliased, reject_code=code,
                         reject_message=message, **repair_inputs)
    except (AIPolicyBlocked, AIQuotaExhausted, AIUnavailable, AIInvalidOutput) as exc:
        return _none(_ai_failure(exc))
    proposal, code2, _msg2, _raw2 = _postprocess(data, ctx, card)
    if proposal is None:
        return _none("ai_invalid_output",
                     explanation=f"The generated query was still invalid after one repair ({code2}).")
    return proposal


def repair(question: str, bad_sql: str, reject_code: str, ctx: QuestionContext, *, reject_message: str = "",
           today: date | None = None) -> SqlProposal:
    """ONE repair call after a guard rejection caused by a model error. Security rejections are never retried."""
    code = (reject_code or "").strip()
    if code in SECURITY_CODES or code not in REPAIRABLE_CODES:
        return SqlProposal(sql=None, source="none", blocked_reason="not_repairable",
                           explanation=f"The query was rejected ({code}); this kind of rejection is not retried.")
    today = _today(today)
    card, inputs = _render_inputs(question, ctx, today)
    aliased = bad_sql or ""
    ast, issue = parse_select(aliased)
    if not issue:
        aliased = render(rename_identifiers(ast, card.real_to_alias))  # never reveal PII column names
    elif card.real_to_alias:
        aliased = ""  # unparsable SQL could contain PII column names verbatim: do not forward it
    with request_trace() as ids:
        result = _repair_call(question, aliased, code, reject_message, ctx, today, card, inputs)
        return _with_ids(result, ids)


def remember(question: str, sql: str, ctx: QuestionContext, *, source: str = "llm",
             explanation: str | None = None, assumptions: tuple[str, ...] = (), chart_hint: ChartHint | None = None,
             today: date | None = None, dataset_version_id: UUID | None = None):
    """Cache an answer that passed the guard and executed (unverified). Returns the cache row or None."""
    try:
        return cache.store(question, sql, ctx, slots=extract_slots(question, _today(today)), source=source,
                           explanation=explanation, assumptions=assumptions, chart_hint=chart_hint,
                           user_id=ctx.user_id, dataset_version_id=dataset_version_id)
    except ValueError:
        log.info("nl2sql: answer not cached (outside policy or invalid)")
        return None


def store_verified(question: str, sql: str, ctx: QuestionContext, *, explanation: str | None = None,
                   assumptions: tuple[str, ...] = (), chart_hint: ChartHint | None = None,
                   today: date | None = None, dataset_version_id: UUID | None = None):
    """Promote a verified (thumbs-up) answer into the cache. Raises ValueError for SQL outside the policy."""
    return cache.store(question, sql, ctx, slots=extract_slots(question, _today(today)), source="verified",
                       verified=True, explanation=explanation, assumptions=assumptions, chart_hint=chart_hint,
                       user_id=ctx.user_id, dataset_version_id=dataset_version_id)


def compute_insights(columns, rows):
    from .insights import compute_insights as _ci

    return _ci(columns, rows)


def narrate(question: str, insights: list[dict]) -> str:
    from .insights import narrate_template

    return narrate_template(question, insights)
