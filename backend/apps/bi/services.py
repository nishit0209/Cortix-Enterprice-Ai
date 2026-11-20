"""Public service API of apps.bi (Conversational BI orchestration). Other apps import only from here.

    start_turn(conversation, question, ...) -> Turn       create the user + pending assistant messages
    ask(turn) -> Iterator[(event, data)]                  the chat pipeline; drives both the SSE and the JSON path
    run_query(...) -> dict                                manual / saved-query SQL through the SAME guard + audit
    export_message(...) -> ExportFile                     re-executes the audited query under the export policy
    apply_feedback(...), dashboard_insights(...)

Pipeline of `ask` (AI_ARCHITECTURE §6, CONTRACT "W3: Conversational BI"):
  authorize (bi.chat is checked by the view; dataset ACL `query` here) -> resolve (semantics + effective
  policy + value-free history) -> generate (nl2sql.propose) -> validate (sqlguard; ONE nl2sql.repair for a
  repairable rejection) -> execute (sqlguard, warehouse) -> explain (chart + local insights + template
  narrative) -> audit / access history / nl2sql.remember.

`ask` never holds a database transaction across a `yield`: each step runs in its own short
`tenant_context(org_id)` block, so the SSE response can stream outside the request transaction.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.core import audit as core_audit
from apps.core.exceptions import Conflict, NexoraError
from apps.core.tenancy import TenantInfo, tenant_context
from apps.core.tenancy.context import current_actor
from apps.core.tenancy.models import uuid7
from nexora.contracts import (
    GUARD_REJECT_CODES,
    REPAIRABLE_REJECT_CODES,
    DatasetSemantics,
    GuardResult,
    QueryPolicy,
    QueryTarget,
    QuestionContext,
    ResultSet,
    SqlProposal,
)

from . import ports
from .models import DEFAULT_TITLE, BiQueryAudit, Conversation, Message
from .results import (
    chart_hint_from_dict,
    choose_chart,
    column_dicts,
    csv_cell,
    jsonable,
    result_bytes,
    serialize_result,
    xlsx_cell,
)

log = logging.getLogger("nexora.bi")

HISTORY_TURNS = 5
MAX_QUESTION_CHARS = 2000
MAX_SQL_CHARS = 20_000
POLICY_BLOCK_CODES = frozenset({"ai_policy_blocked", "ai_quota_exhausted", "ai_not_configured"})
CACHE_SOURCES = frozenset({"cache_exact", "cache_semantic"})
EXEC_REJECT_CODES = frozenset({"cost_exceeded", "plan_relation_not_allowed"})  # execute-stage policy rejections
STAGES = ("authorizing", "resolving", "generating", "validating", "executing", "explaining")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

GENERIC_ERRORS = {
    "not_found": "This dataset is not available to you.",
    "internal_error": "Something went wrong while answering. Please try again.",
    "cancelled": "The request was cancelled.",
    "no_sql": "I couldn't turn that into a query. Try rephrasing, or write the SQL manually.",
    "query_failed": "The query could not be executed.",
}


# =============================================================================================== errors
class DatasetNotFound(NexoraError):
    status_code = 404
    default_code = "not_found"
    default_detail = "Not found."


class QueryRejected(NexoraError):
    """A guard rejection or an execution failure. `code` is the guard reject code (GUARD_REJECT_CODES) or the
    executor's error code; the problem body carries `query_audit_id` (the rejection is audited)."""

    status_code = 422
    default_code = "sql_rejected"
    default_detail = "The SQL was rejected by the security guard."

    def __init__(self, code: str, detail: str | None = None, *, status: int = 422, extra: dict | None = None):
        super().__init__(detail or self.default_detail, code=code, extra=extra)
        self.status_code = status


# =============================================================================================== scope
@dataclass(frozen=True)
class DatasetScope:
    """Everything a query on one dataset needs: the semantic description and the caller's frozen policy."""

    semantics: DatasetSemantics
    policy: QueryPolicy

    @property
    def dataset_id(self) -> UUID:
        return self.semantics.dataset_id


def _semantics(dataset_id: UUID) -> DatasetSemantics:
    try:
        return ports.dataset_semantics(dataset_id)
    except NexoraError:
        raise  # keeps the semantic layer's own code (e.g. 404 not_found, 409 dataset_not_ready)
    except (ObjectDoesNotExist, LookupError, Http404, ValueError) as exc:
        raise DatasetNotFound() from exc


def resolve_dataset(user_id: UUID, dataset_id: UUID, *, privilege: str = "query") -> DatasetScope:
    """Dataset ACL + semantics + effective policy (inside a tenant context). Invisible/unknown -> 404."""
    if not ports.check_dataset_access(user_id, dataset_id, privilege):
        raise DatasetNotFound()
    semantics = _semantics(dataset_id)
    policy = ports.effective_policy(user_id, dataset_id, semantics.tables, semantics.columns_by_table,
                                    privilege=privilege)
    return DatasetScope(semantics=semantics, policy=policy)


def ensure_dataset_visible(user_id: UUID, dataset_id: UUID, *, privilege: str = "query") -> DatasetSemantics:
    if not ports.check_dataset_access(user_id, dataset_id, privilege):
        raise DatasetNotFound()
    return _semantics(dataset_id)


def build_context(*, org_id: UUID, user_id: UUID, scope: DatasetScope, history: tuple[dict, ...] = ()) -> QuestionContext:
    """QuestionContext with columns/metrics filtered to what the policy allows (nothing denied reaches nl2sql)."""
    sem, policy = scope.semantics, scope.policy
    allowed = policy.allowed_columns
    columns_meta: dict[str, tuple[dict, ...]] = {}
    for table, metas in sem.columns_meta.items():
        ok = allowed.get(table)
        if not ok:
            continue
        kept = []
        for meta in metas:
            if meta.get("name") in ok:
                item = dict(meta)
                item["masked"] = (table, meta.get("name")) in policy.column_masks
                kept.append(item)
        columns_meta[table] = tuple(kept)
    denied = {c for t, cols in sem.columns_by_table.items() for c in cols if c not in allowed.get(t, frozenset())}
    denied_tables = {t for t in sem.tables if t not in allowed}
    metrics = tuple(
        dict(m) for m in sem.metrics
        if not (set(_IDENT.findall(str(m.get("expression") or ""))) & (denied | denied_tables))
    )
    return QuestionContext(
        org_id=org_id,
        user_id=user_id,
        dataset_id=sem.dataset_id,
        policy=policy,
        history=history,
        columns_meta=columns_meta,
        metrics=metrics,
        dataset_name=sem.dataset_name,
        dataset_classification=sem.classification,
    )


# =============================================================================================== guarded execution
@dataclass
class AuditInfo:
    org_id: UUID
    user_id: UUID | None
    api_key_id: UUID | None
    dataset_id: UUID
    dataset_name: str = ""
    purpose: str = "chat"
    question: str = ""
    source: str = "manual"
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    ai_request_ids: list[UUID] = field(default_factory=list)


@dataclass
class ExecOutcome:
    ok: bool
    audit_id: UUID
    verdict: str  # ok | rejected | error
    code: str = ""
    message: str = ""
    status: int = 422
    guard: GuardResult | None = None
    result: ResultSet | None = None


def guard_sql(sql: str, policy: QueryPolicy) -> GuardResult:
    """sqlguard.validate, failing closed: an exception inside the guard is a rejection, never a pass."""
    try:
        result = ports.validate(sql, policy)
    except NexoraError as exc:
        return GuardResult(ok=False, reject_code=_code_of(exc)[0], message=str(exc.detail), executed_sql=None, params={})
    except Exception:  # noqa: BLE001 - fail closed
        log.exception("bi: sqlguard.validate raised")
        return GuardResult(ok=False, reject_code="invalid_sql", message="The query could not be validated.",
                           executed_sql=None, params={})
    if result.ok and not result.executed_sql:
        return GuardResult(ok=False, reject_code="invalid_sql", message="The guard returned no executable SQL.",
                           executed_sql=None, params={})
    return result


def _code_of(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, APIException):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else getattr(exc, "default_code", "query_failed")
        return str(code), int(getattr(exc, "status_code", 422))
    code = getattr(exc, "reject_code", None) or getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code, 422
    return "query_failed", 422


def execute_guarded(guard: GuardResult, policy: QueryPolicy, org_id: UUID) -> tuple[ResultSet | None, str, str, int]:
    """(result, code, message, http_status). Never raises for query-level failures."""
    try:
        rs = ports.execute(guard, policy, QueryTarget(kind="warehouse", org_id=org_id))
        return rs, "", "", 200
    except Exception as exc:  # noqa: BLE001 - every failure becomes an audited verdict
        code, status = _code_of(exc)
        message = str(getattr(exc, "detail", "") or "") or GENERIC_ERRORS["query_failed"]
        if code == "query_failed":
            log.warning("bi: query execution failed (%s)", type(exc).__name__)
        return None, code, message, status


def write_audit(audit_id: UUID, info: AuditInfo, *, raw_sql: str, verdict: str, policy: QueryPolicy,
                guard: GuardResult | None = None, result: ResultSet | None = None, code: str = "",
                message: str = "", started_at=None, duration_ms: int | None = None) -> BiQueryAudit:
    """Insert the append-only audit row (inside a tenant context). Never stores result values."""
    return BiQueryAudit.objects.create(
        id=audit_id,
        occurred_at=started_at or timezone.now(),
        user_id=info.user_id,
        api_key_id=info.api_key_id,
        dataset_id=info.dataset_id,
        dataset_name=info.dataset_name[:200],
        conversation_id=info.conversation_id,
        message_id=info.message_id,
        purpose=info.purpose,
        question=info.question[:MAX_QUESTION_CHARS],
        source=info.source,
        raw_sql=(raw_sql or "")[:MAX_SQL_CHARS * 2],
        executed_sql=(guard.executed_sql if guard and guard.executed_sql else "") or "",
        params=jsonable(dict(guard.params)) if guard and guard.params else {},
        verdict=verdict,
        reject_code=code[:64],
        message=(message or "")[:2000],
        policy_fingerprint=(policy.fingerprint or "")[:128],
        executing_role=ports.executing_role(info.org_id)[:128] if verdict == "ok" else "",
        target="warehouse",
        plan_cost=result.plan_cost if result is not None else None,
        duration_ms=result.duration_ms if result is not None else duration_ms,
        row_count=result.row_count if result is not None else None,
        bytes=result_bytes(result) if result is not None else None,
        truncated=result.truncated if result is not None else None,
        ai_request_ids=list(info.ai_request_ids),
    )


def run_guarded(sql: str, scope: DatasetScope, info: AuditInfo) -> ExecOutcome:
    """validate -> (audit rejection) -> execute -> audit (+ access history). Inside a tenant context."""
    started = timezone.now()
    t0 = time.monotonic()
    guard = guard_sql(sql, scope.policy)
    audit_id = uuid7()
    if not guard.ok:
        code = guard.reject_code or "invalid_sql"
        write_audit(audit_id, info, raw_sql=sql, verdict="rejected", policy=scope.policy, guard=guard, code=code,
                    message=guard.message, started_at=started, duration_ms=_ms(t0))
        return ExecOutcome(ok=False, audit_id=audit_id, verdict="rejected", code=code, message=guard.message,
                           guard=guard)
    rs, code, message, status = execute_guarded(guard, scope.policy, info.org_id)
    if rs is None:
        verdict = "rejected" if code in EXEC_REJECT_CODES else "error"
        write_audit(audit_id, info, raw_sql=sql, verdict=verdict, policy=scope.policy, guard=guard, code=code,
                    message=message, started_at=started, duration_ms=_ms(t0))
        return ExecOutcome(ok=False, audit_id=audit_id, verdict=verdict, code=code, message=message, status=status,
                           guard=guard)
    write_audit(audit_id, info, raw_sql=sql, verdict="ok", policy=scope.policy, guard=guard, result=rs,
                started_at=started)
    ports.record_access(info.dataset_id, "export" if info.purpose == "export" else "query", user_id=info.user_id,
                        api_key_id=info.api_key_id, query_audit_id=audit_id,
                        metadata={"purpose": info.purpose, "row_count": rs.row_count})
    return ExecOutcome(ok=True, audit_id=audit_id, verdict="ok", guard=guard, result=rs)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def query_response(outcome: ExecOutcome, *, question: str = "", chart_hint=None) -> dict:
    result = serialize_result(outcome.result)
    return {
        "query_audit_id": str(outcome.audit_id),
        "executed_sql": outcome.guard.executed_sql,
        **result,
        "chart": choose_chart(chart_hint, result["columns"], result["rows"], question=question),
    }


def run_query(*, org_id: UUID, user_id: UUID, api_key_id: UUID | None, dataset_id: UUID, sql: str,
              purpose: str = "manual", question: str = "", chart_hint=None, source: str = "manual") -> dict:
    """Manual / saved / dashboard SQL through the same guard + execute + audit path as chat (`source` manual or
    dashboard). Raises QueryRejected (audited) on a rejection/failure. Inside the request's tenant context."""
    scope = resolve_dataset(user_id, dataset_id, privilege="query")
    info = AuditInfo(org_id=org_id, user_id=user_id, api_key_id=api_key_id, dataset_id=scope.dataset_id,
                     dataset_name=scope.semantics.dataset_name, purpose=purpose, question=question, source=source)
    outcome = run_guarded(sql, scope, info)
    if not outcome.ok:
        raise QueryRejected(outcome.code, outcome.message or None, status=outcome.status,
                            extra={"query_audit_id": str(outcome.audit_id)})
    return query_response(outcome, question=question, chart_hint=chart_hint)


def check_sql(*, org_id: UUID, user_id: UUID, api_key_id: UUID | None, dataset_id: UUID, sql: str,
              purpose: str = "saved_query", question: str = "") -> GuardResult:
    """Validate (no execution) under the caller's policy; a rejection is audited and raised."""
    scope = resolve_dataset(user_id, dataset_id, privilege="query")
    guard = guard_sql(sql, scope.policy)
    if not guard.ok:
        info = AuditInfo(org_id=org_id, user_id=user_id, api_key_id=api_key_id, dataset_id=scope.dataset_id,
                         dataset_name=scope.semantics.dataset_name, purpose=purpose, question=question,
                         source="manual")
        audit_id = uuid7()
        write_audit(audit_id, info, raw_sql=sql, verdict="rejected", policy=scope.policy, guard=guard,
                    code=guard.reject_code or "invalid_sql", message=guard.message)
        raise QueryRejected(guard.reject_code or "invalid_sql", guard.message or None,
                            extra={"query_audit_id": str(audit_id)})
    return guard


# =============================================================================================== chat turn
@dataclass(frozen=True)
class Turn:
    org_id: UUID
    user_id: UUID
    conversation_id: UUID
    dataset_id: UUID
    question: str
    user_message_id: UUID
    message_id: UUID  # the assistant message this turn fills
    api_key_id: UUID | None = None
    tenant: TenantInfo | None = None

    @property
    def actor(self) -> tuple[str, UUID | None]:
        return ("api_key", self.api_key_id) if self.api_key_id else ("user", self.user_id)


def create_conversation(*, user_id: UUID, dataset_id: UUID, title: str = "", request=None) -> Conversation:
    """A new conversation about a dataset the user can query (404 otherwise). Inside a tenant context."""
    semantics = ensure_dataset_visible(user_id, dataset_id, privilege="query")
    conv = Conversation.objects.create(user_id=user_id, dataset_id=dataset_id, title=(title or "")[:200] or DEFAULT_TITLE)
    core_audit.record("bi.conversation.create", target=conv, request=request,
                      metadata={"dataset_id": str(dataset_id), "dataset_name": semantics.dataset_name})
    return conv


def get_conversation(user_id: UUID, conversation_id) -> Conversation | None:
    """The user's own conversation (others' conversations do not exist for them)."""
    return Conversation.objects.filter(id=conversation_id, user_id=user_id).first()


def delete_conversation(*, user_id: UUID, conversation_id, request=None) -> bool:
    conv = get_conversation(user_id, conversation_id)
    if conv is None:
        return False
    core_audit.record("bi.conversation.delete", target=conv, request=request,
                      metadata={"dataset_id": str(conv.dataset_id)})
    conv.delete()
    return True


def start_turn(conversation: Conversation, question: str, *, user_id: UUID, api_key_id: UUID | None = None,
               tenant: TenantInfo | None = None) -> Turn:
    """Create the user message and the pending assistant message (inside the request's tenant context)."""
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    now = timezone.now()
    user_msg = Message.objects.create(conversation=conversation, role="user", content=question, status="ok")
    assistant = Message.objects.create(conversation=conversation, role="assistant", status="pending",
                                       question=question)
    fields = {"last_message_at": now, "updated_at": now}
    if conversation.title == DEFAULT_TITLE:
        fields["title"] = _title_from(question)
    Conversation.objects.filter(id=conversation.id).update(**fields)
    return Turn(org_id=conversation.org_id, user_id=user_id, conversation_id=conversation.id,
                dataset_id=conversation.dataset_id, question=question, user_message_id=user_msg.id,
                message_id=assistant.id, api_key_id=api_key_id, tenant=tenant)


def _title_from(question: str) -> str:
    text = " ".join(question.split())
    return (text[:77] + "...") if len(text) > 80 else (text or DEFAULT_TITLE)


@contextmanager
def _step(turn: Turn):
    """One short tenant transaction with the turn's actor (the SSE body runs outside the request)."""
    token = current_actor.set(turn.actor)
    try:
        with tenant_context(turn.org_id, info=turn.tenant):
            yield
    finally:
        current_actor.reset(token)


def history_for(conversation_id: UUID, *, exclude: UUID | None = None, turns: int = HISTORY_TURNS) -> tuple[dict, ...]:
    """The last answered turns WITHOUT values: question, SQL and column names only."""
    qs = Message.objects.filter(conversation_id=conversation_id, role="assistant", status="ok").exclude(sql_raw="")
    if exclude is not None:
        qs = qs.exclude(id=exclude)
    recent = list(qs.order_by("-created_at").values("question", "sql_raw", "result")[:turns])
    out = []
    for m in reversed(recent):
        cols = [c.get("name") for c in ((m["result"] or {}).get("columns") or []) if isinstance(c, dict)]
        out.append({"question": m["question"], "sql": m["sql_raw"], "columns": cols})
    return tuple(out)


class _Chat:
    """State of one `ask` run. Every DB touch goes through `_step` (no transaction spans a yield)."""

    def __init__(self, turn: Turn):
        self.turn = turn
        self.finished = False
        self.ai_ids: list[UUID] = []
        self.scope: DatasetScope | None = None
        self.ctx: QuestionContext | None = None

    # -- message finalisation ------------------------------------------------------------------------------
    def finish(self, **fields) -> None:
        now = timezone.now()
        with _step(self.turn):
            Message.objects.filter(id=self.turn.message_id).update(updated_at=now, **fields)
            Conversation.objects.filter(id=self.turn.conversation_id).update(last_message_at=now, updated_at=now)
        self.finished = True

    def fail(self, code: str, detail: str | None, *, status: str = "error", audit_id: UUID | None = None,
             extra_fields: dict | None = None, extra_data: dict | None = None) -> tuple[str, dict]:
        detail = detail or GENERIC_ERRORS.get(code) or "The question could not be answered."
        self.finish(status=status, error_code=code[:64], content=detail, query_audit_id=audit_id,
                    **(extra_fields or {}))
        data = {"code": code, "detail": detail, "message_id": str(self.turn.message_id)}
        if audit_id is not None:
            data["query_audit_id"] = str(audit_id)
        if extra_data:
            data.update(extra_data)
        return ("error", data)

    def info(self, source: str) -> AuditInfo:
        sem = self.scope.semantics
        return AuditInfo(org_id=self.turn.org_id, user_id=self.turn.user_id, api_key_id=self.turn.api_key_id,
                         dataset_id=sem.dataset_id, dataset_name=sem.dataset_name, purpose="chat",
                         question=self.turn.question, source=source, conversation_id=self.turn.conversation_id,
                         message_id=self.turn.message_id, ai_request_ids=list(self.ai_ids))

    # -- pipeline -----------------------------------------------------------------------------------------
    def run(self) -> Iterator[tuple[str, dict]]:
        turn = self.turn
        yield ("status", {"stage": "authorizing"})
        try:
            with _step(turn):
                if not ports.check_dataset_access(turn.user_id, turn.dataset_id, "query"):
                    raise DatasetNotFound()
        except DatasetNotFound:
            yield self.fail("not_found", None)
            return

        yield ("status", {"stage": "resolving"})
        try:
            with _step(turn):
                self.scope = resolve_dataset(turn.user_id, turn.dataset_id, privilege="query")
                history = history_for(turn.conversation_id, exclude=turn.message_id)
                self.ctx = build_context(org_id=turn.org_id, user_id=turn.user_id, scope=self.scope, history=history)
        except NexoraError as exc:
            code, _status = _code_of(exc)
            yield self.fail(code, str(exc.detail) if code != "not_found" else None)
            return

        yield ("status", {"stage": "generating"})
        with _step(turn):
            proposal = ports.propose(turn.question, self.ctx, semantics=self.scope.semantics)
        self.ai_ids.extend(proposal.ai_request_ids)
        if proposal.sql is None or not str(proposal.sql).strip():
            yield from self._no_sql(proposal)
            return

        sql, source = proposal.sql, proposal.source
        assumptions = list(proposal.assumptions)
        chart_hint, explanation = proposal.chart_hint, proposal.explanation or ""
        repaired = False
        while True:
            yield ("status", {"stage": "validating", "source": source})
            started, t0 = timezone.now(), time.monotonic()
            with _step(turn):
                guard = guard_sql(sql, self.scope.policy)
            audit_id = uuid7()
            failure: tuple[str, str, int, str] | None = None  # (code, message, http status, verdict)
            rs = None
            if not guard.ok:
                failure = (guard.reject_code or "invalid_sql", guard.message, 422, "rejected")
            else:
                yield ("sql", {"raw_sql": sql, "executed_sql": guard.executed_sql, "source": source,
                               "query_audit_id": str(audit_id), "assumptions": assumptions,
                               "tables": list(guard.tables)})
                yield ("status", {"stage": "executing", "source": source})
                with _step(turn):
                    rs, code, message, status = execute_guarded(guard, self.scope.policy, turn.org_id)
                if rs is None:
                    failure = (code, message, status, "rejected" if code in EXEC_REJECT_CODES else "error")
            if failure is None:
                break
            code, message, _http, verdict = failure
            with _step(turn):
                write_audit(audit_id, self.info(source), raw_sql=sql, verdict=verdict, policy=self.scope.policy,
                            guard=guard, code=code, message=message, started_at=started, duration_ms=_ms(t0))
            if code in REPAIRABLE_REJECT_CODES and not repaired:
                repaired = True
                yield ("status", {"stage": "generating", "source": "llm", "repair": True})
                with _step(turn):
                    fixed = ports.repair(turn.question, sql, code, self.ctx, reject_message=message or "")
                self.ai_ids.extend(fixed.ai_request_ids)
                if fixed.sql and str(fixed.sql).strip():
                    sql, source = fixed.sql, (fixed.source if fixed.source != "none" else "llm")
                    assumptions = list(fixed.assumptions) or assumptions
                    chart_hint = fixed.chart_hint or chart_hint
                    explanation = fixed.explanation or explanation
                    continue
            yield self.fail(code, message or None, audit_id=audit_id,
                            extra_fields={"sql_raw": sql, "sql_source": source,
                                          "sql_executed": guard.executed_sql or ""})
            return

        with _step(turn):
            write_audit(audit_id, self.info(source), raw_sql=sql, verdict="ok", policy=self.scope.policy, guard=guard,
                        result=rs, started_at=started)
            ports.record_access(turn.dataset_id, "query", user_id=turn.user_id, api_key_id=turn.api_key_id,
                                query_audit_id=audit_id, metadata={"purpose": "chat", "row_count": rs.row_count})

        result = serialize_result(rs)
        chart = choose_chart(chart_hint, result["columns"], result["rows"], question=turn.question)
        yield ("result", {**result, "chart": chart})

        yield ("status", {"stage": "explaining"})
        insights = self._insights(rs)
        for item in insights:
            yield ("insight", item)
        narrative = ports.narrate(turn.question, insights, row_count=rs.row_count)
        if explanation and not insights:
            narrative = f"{explanation} {narrative}".strip()
        yield ("token", {"text": narrative})

        self.finish(status="ok", error_code="", content=narrative, sql_raw=sql, sql_executed=guard.executed_sql,
                    sql_source=source, result=result, chart=chart, insights=insights, assumptions=assumptions,
                    explanation=explanation, query_audit_id=audit_id,
                    chart_hint=_hint_json(chart_hint), dataset_version_id=self.scope.semantics.dataset_version_id)
        if source not in CACHE_SOURCES:
            self._remember(sql, source, explanation, assumptions, chart_hint)
        yield ("done", {"message_id": str(turn.message_id), "status": "ok", "ai_requests_used": len(self.ai_ids)})

    def _no_sql(self, proposal: SqlProposal) -> Iterator[tuple[str, dict]]:
        turn = self.turn
        if proposal.clarification and not proposal.blocked_reason:
            self.finish(status="clarification", error_code="", content=proposal.clarification,
                        sql_source=proposal.source, assumptions=list(proposal.assumptions))
            yield ("token", {"text": proposal.clarification})
            yield ("done", {"message_id": str(turn.message_id), "status": "clarification",
                            "ai_requests_used": len(self.ai_ids)})
            return
        code = proposal.blocked_reason or "no_sql"
        detail = proposal.explanation or ports.blocked_message(code) or GENERIC_ERRORS["no_sql"]
        status = "blocked" if code in POLICY_BLOCK_CODES else "error"
        extra: dict[str, Any] = {}
        with _step(turn):
            audit_id = uuid7()
            write_audit(audit_id, self.info("none"), raw_sql="", verdict="blocked" if status == "blocked" else "error",
                        policy=self.scope.policy, code=code, message=detail)
            if code == "ai_quota_exhausted":  # CONTRACT "AI quota UX": the client shows when it resets
                extra["resets_at"] = getattr(proposal, "resets_at", None) or ports.ai_quota_resets_at(turn.user_id)
        yield self.fail(code, detail, status=status, audit_id=audit_id, extra_data=extra)

    def _insights(self, rs: ResultSet) -> list[dict]:
        try:
            raw = ports.compute_insights(column_dicts(rs.columns), [list(r) for r in rs.rows]) or []
        except Exception:  # noqa: BLE001 - insights are best effort; the answer stands without them
            log.warning("bi: insight computation failed", exc_info=True)
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            entry = {"kind": str(item.get("kind") or "note"), "text": str(item["text"])}
            if item.get("data") is not None:
                entry["data"] = jsonable(item["data"])
            out.append(entry)
        return out

    def _remember(self, sql: str, source: str, explanation: str, assumptions: list, chart_hint) -> None:
        try:
            with _step(self.turn):  # its own (savepoint) transaction: a cache failure rolls back alone
                ports.remember(self.turn.question, sql, self.ctx, source=source, explanation=explanation or None,
                               assumptions=tuple(assumptions), chart_hint=chart_hint,
                               semantics=self.scope.semantics)
        except Exception:  # noqa: BLE001 - caching never fails an answer
            log.warning("bi: nl2sql.remember failed", exc_info=True)


def _hint_json(hint) -> dict | None:
    if hint is None:
        return None
    return {"type": hint.type, "x": hint.x, "y": list(hint.y or ()), "series": hint.series}


def ask(turn: Turn) -> Iterator[tuple[str, dict]]:
    """The chat pipeline as a generator of (event, data) — CONTRACT "Chat SSE events".

    Ends with exactly one terminal event: `done {message_id, status, ai_requests_used}` or
    `error {code, detail, message_id, query_audit_id?}`. The assistant message is always finalised (also when
    the consumer disconnects: status error / `cancelled`)."""
    chat = _Chat(turn)
    try:
        yield from chat.run()
    except GeneratorExit:
        if not chat.finished:
            _safe_fail(chat, "cancelled")
        raise
    except Exception:  # noqa: BLE001 - never leak internals into the stream
        log.exception("bi: chat turn failed")
        if not chat.finished:
            event = _safe_fail(chat, "internal_error")
            if event:
                yield event
                return
        yield ("error", {"code": "internal_error", "detail": GENERIC_ERRORS["internal_error"],
                         "message_id": str(turn.message_id)})


def _safe_fail(chat: _Chat, code: str):
    try:
        return chat.fail(code, None)
    except Exception:  # noqa: BLE001
        log.exception("bi: could not finalise message %s", chat.turn.message_id)
        return None


def run_to_completion(turn: Turn) -> list[tuple[str, dict]]:
    """The synchronous JSON path: drive the same generator to the end."""
    return list(ask(turn))


# =============================================================================================== feedback
def apply_feedback(message: Message, value: str, *, user_id: UUID) -> dict:
    """Store 👍/👎. A thumbs-up promotes the answer into the nl2sql cache as VERIFIED, re-checked against the
    user's CURRENT policy (nl2sql raises ValueError when the SQL is outside it)."""
    verified = False
    reason = ""
    if value == "up" and message.status == "ok" and message.sql_raw:
        try:
            scope = resolve_dataset(user_id, message.conversation.dataset_id, privilege="query")
            ctx = build_context(org_id=message.org_id, user_id=user_id, scope=scope)
            with transaction.atomic():
                ports.store_verified(message.question, message.sql_raw, ctx,
                                     explanation=message.explanation or None,
                                     assumptions=tuple(message.assumptions or ()),
                                     chart_hint=chart_hint_from_dict(message.chart_hint),
                                     semantics=scope.semantics)
            verified = True
        except (ValueError, NexoraError) as exc:
            reason = "outside_policy" if isinstance(exc, ValueError) else _code_of(exc)[0]
        except Exception:  # noqa: BLE001 - feedback itself must still be stored
            log.warning("bi: store_verified failed", exc_info=True)
            reason = "cache_unavailable"
    Message.objects.filter(id=message.id).update(feedback=value, updated_at=timezone.now())
    core_audit.record("bi.message.feedback", target=message,
                      metadata={"value": value, "verified": verified, "query_audit_id": str(message.query_audit_id or "")})
    body = {"message_id": str(message.id), "feedback": value, "verified": verified}
    if reason:
        body["not_verified_reason"] = reason
    return body


# =============================================================================================== exports
@dataclass
class ExportFile:
    filename: str
    content_type: str
    body: Any  # iterator of str/bytes (csv) or bytes (xlsx)
    row_count: int
    query_audit_id: UUID


EXPORT_TYPES = {"csv": "text/csv; charset=utf-8",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def export_message(*, org_id: UUID, user_id: UUID, api_key_id: UUID | None, message_id: UUID, fmt: str) -> ExportFile:
    """Re-run the AUDITED query of an answered message under the caller's CURRENT export policy and build the
    file. Stored rows are never trusted: the audited raw SQL is re-validated by the guard and re-executed
    (policies may have tightened since). Formula-injection cells are escaped. Inside a tenant context."""
    if fmt not in EXPORT_TYPES:
        raise NexoraError("Unsupported export format.", code="invalid")
    message = (Message.objects.select_related("conversation")
               .filter(id=message_id, role="assistant", conversation__user_id=user_id).first())
    if message is None:
        raise DatasetNotFound()
    if message.status != "ok" or not message.query_audit_id:
        raise Conflict("This message has no query result to export.", code="export_not_available")
    source_audit = BiQueryAudit.objects.filter(id=message.query_audit_id, verdict="ok").first()
    if source_audit is None or not source_audit.raw_sql:
        raise Conflict("This message has no query result to export.", code="export_not_available")
    scope = resolve_dataset(user_id, message.conversation.dataset_id, privilege="export")
    info = AuditInfo(org_id=org_id, user_id=user_id, api_key_id=api_key_id, dataset_id=scope.dataset_id,
                     dataset_name=scope.semantics.dataset_name, purpose="export", question=message.question,
                     source=source_audit.source, conversation_id=message.conversation_id, message_id=message.id)
    outcome = run_guarded(source_audit.raw_sql, scope, info)
    if not outcome.ok:
        raise QueryRejected(outcome.code, outcome.message or None, status=outcome.status,
                            extra={"query_audit_id": str(outcome.audit_id)})
    rs = outcome.result
    core_audit.record("bi.export", target=message,
                      metadata={"format": fmt, "row_count": rs.row_count, "truncated": rs.truncated,
                                "query_audit_id": str(outcome.audit_id), "dataset_id": str(scope.dataset_id)})
    stem = re.sub(r"[^A-Za-z0-9]+", "-", message.conversation.title or "export").strip("-").lower()[:60] or "export"
    filename = f"{stem}-{timezone.now():%Y%m%d-%H%M}.{fmt}"
    header = [c.name for c in rs.columns]
    body = _csv_stream(header, rs.rows) if fmt == "csv" else _xlsx_bytes(header, rs.rows)
    return ExportFile(filename=filename, content_type=EXPORT_TYPES[fmt], body=body, row_count=rs.row_count,
                      query_audit_id=outcome.audit_id)


class _Echo:
    def write(self, value: str) -> str:
        return value


def _csv_stream(header: list[str], rows) -> Iterator[str]:
    writer = csv.writer(_Echo())
    yield "﻿"  # BOM: spreadsheet apps detect UTF-8
    yield writer.writerow([csv_cell(h) for h in header])
    for row in rows:
        yield writer.writerow([csv_cell(v) for v in row])


def _xlsx_bytes(header: list[str], rows) -> bytes:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Result")
    ws.append([xlsx_cell(h) for h in header])
    for row in rows:
        ws.append([xlsx_cell(v) for v in row])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================================== dashboard insights
KIND_TITLES = {"top_n": "Top performer", "share": "Concentration", "period_change": "Period change",
               "trend": "Trend", "anomaly": "Anomaly"}


def dashboard_insights(user_id: UUID, *, limit: int = 12, days: int = 30) -> list[dict]:
    """Latest locally computed insights from the caller's OWN recent answers, limited to datasets the caller
    can still read. (Other users' answers are never shown: they were computed under THEIR policy.)"""
    since = timezone.now() - timedelta(days=days)
    msgs = list(
        Message.objects.select_related("conversation")
        .filter(role="assistant", status="ok", conversation__user_id=user_id, created_at__gte=since)
        .exclude(insights=[])
        .order_by("-created_at")[:50]
    )
    if not msgs:
        return []
    visible = ports.visible_dataset_ids(user_id, {m.conversation.dataset_id for m in msgs}, "read")
    out: list[dict] = []
    for m in msgs:
        if str(m.conversation.dataset_id) not in visible:
            continue
        for item in m.insights or []:
            kind = str(item.get("kind") or "note")
            out.append({
                "kind": kind,
                "title": KIND_TITLES.get(kind, "Insight"),
                "text": str(item.get("text") or ""),
                "dataset_id": str(m.conversation.dataset_id),
                "conversation_id": str(m.conversation_id),
                "message_id": str(m.id),
                "question": m.question,
                "created_at": m.created_at,
            })
            if len(out) >= limit:
                return out
    return out


# Dashboards (CONTRACT "W3 additions"): implemented in apps.bi.dashboards, public through this module.
from .dashboards import (  # noqa: E402
    add_widget,
    create_dashboard,
    dashboard_payload,
    delete_dashboard,
    delete_widget,
    get_owned,
    get_visible,
    pin_message,
    update_dashboard,
    update_widget,
    visible_dashboards,
    widget_data,
)

__all__ = [
    "GUARD_REJECT_CODES",
    "add_widget",
    "create_conversation",
    "create_dashboard",
    "delete_conversation",
    "get_conversation",
    "get_owned",
    "get_visible",
    "visible_dashboards",
    "dashboard_payload",
    "delete_dashboard",
    "delete_widget",
    "pin_message",
    "update_dashboard",
    "update_widget",
    "widget_data",
    "STAGES",
    "DatasetNotFound",
    "DatasetScope",
    "ExportFile",
    "QueryRejected",
    "Turn",
    "apply_feedback",
    "ask",
    "build_context",
    "check_sql",
    "dashboard_insights",
    "ensure_dataset_visible",
    "export_message",
    "history_for",
    "resolve_dataset",
    "run_query",
    "run_to_completion",
    "start_turn",
]
