"""Persistence of request metadata: AIRequestLog (append-only) + AIUsageDaily counters + a structured log line.

Rows are written through the "durable" DB alias (a separate connection with the same RLS-bound app role) in
their OWN transaction with the tenant setting applied on that connection, so they survive a rolled-back request
transaction (for example a view that lets AIPolicyBlocked propagate and is rolled back on 4xx).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Protocol
from uuid import UUID

from django.db import DEFAULT_DB_ALIAS, DatabaseError, IntegrityError, connections, transaction
from django.db.models import F

from apps.core.tenancy.context import set_db_tenant

from .constants import RequestStatus
from .timeutil import pacific_day

log = logging.getLogger("nexora.ai")

DURABLE_ALIAS = "durable"


@dataclass
class RequestRecord:
    org_id: UUID
    user_id: UUID | None
    feature: str
    model: str
    prompt_key: str
    prompt_version: str
    payload_classes: list[str]
    payload_assets: list[str]
    ai_data_policy: str
    gemini_tier: str
    priority: str
    status: str
    error_code: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 0
    prompt_sha256: str = ""
    prompt_text: str = ""          # redacted; only when the org enabled log_prompt_text
    request_id: str = ""
    extra: dict = field(default_factory=dict)  # structured-log only (redaction counts, scope, ...)

    def log_dict(self) -> dict:
        d = asdict(self)
        d.pop("prompt_text", None)  # NEVER in application logs
        d["org_id"] = str(self.org_id)
        d["user_id"] = str(self.user_id) if self.user_id else None
        return d


class Recorder(Protocol):
    def record(self, rec: RequestRecord) -> UUID | None: ...


def emit_log_line(rec: RequestRecord) -> None:
    level = logging.INFO if rec.status == RequestStatus.OK else logging.WARNING
    log.log(level, "ai_request %s", json.dumps(rec.log_dict(), default=str, sort_keys=True))


def _is_test_mirror_of_default(alias: str) -> bool:
    conn = connections[alias]
    return (conn.settings_dict.get("TEST") or {}).get("MIRROR") == DEFAULT_DB_ALIAS


def _default_in_testcase_transaction() -> bool:
    return any(getattr(a, "_from_testcase", False) for a in connections[DEFAULT_DB_ALIAS].atomic_blocks)


def log_alias() -> str:
    """DB alias for must-survive log rows.

    Always "durable" in production. Falls back to "default" (a savepoint in the caller's transaction) only when
    the alias is not configured, or when "durable" is a test MIRROR of "default" while "default" is held open by
    a django TestCase transaction: the mirror's separate connection could not see the uncommitted org/user rows
    that the log references. Transactional tests (`django_db(transaction=True)`) use the real separate
    connection and prove that rows survive a rollback.
    """
    if DURABLE_ALIAS not in connections.databases:
        return DEFAULT_DB_ALIAS
    if _is_test_mirror_of_default(DURABLE_ALIAS) and _default_in_testcase_transaction():
        return DEFAULT_DB_ALIAS
    return DURABLE_ALIAS


class DbRecorder:
    """Writes the log row + usage counters in their own transaction on the durable alias. Never raises:
    persistence failures are logged (the structured `nexora.ai` line is emitted first and always survives)."""

    def __init__(self, alias: str | None = None):
        self._alias = alias

    def record(self, rec: RequestRecord) -> UUID | None:
        from .models import AIRequestLog, AIUsageDaily

        emit_log_line(rec)
        alias = self._alias or log_alias()
        try:
            with transaction.atomic(using=alias):
                set_db_tenant(rec.org_id, using=alias)
                row = AIRequestLog(
                    org_id=rec.org_id,
                    user_id=rec.user_id,
                    feature=rec.feature[:60],
                    model=rec.model[:80],
                    prompt_key=rec.prompt_key[:80],
                    prompt_version=str(rec.prompt_version)[:20],
                    payload_classes=list(rec.payload_classes),
                    payload_assets=list(rec.payload_assets)[:50],
                    ai_data_policy=rec.ai_data_policy[:20],
                    gemini_tier=rec.gemini_tier[:10],
                    priority=rec.priority[:16],
                    input_tokens=rec.input_tokens,
                    output_tokens=rec.output_tokens,
                    cached_tokens=rec.cached_tokens,
                    latency_ms=rec.latency_ms,
                    attempts=rec.attempts,
                    status=rec.status,
                    error_code=rec.error_code[:60],
                    prompt_sha256=rec.prompt_sha256,
                    prompt_text=rec.prompt_text,
                    request_id=rec.request_id[:64],
                )
                row.save(using=alias, force_insert=True)
                self._bump_usage(AIUsageDaily, rec, alias)
            return row.id
        except DatabaseError:
            log.exception("ai: failed to persist AIRequestLog (status=%s feature=%s)", rec.status, rec.feature)
            return None

    @staticmethod
    def _bump_usage(model_cls, rec: RequestRecord, alias: str = DEFAULT_DB_ALIAS) -> None:
        day = pacific_day()
        network = rec.attempts > 0
        deltas = {
            "requests": rec.attempts if network else 0,
            "input_tokens": rec.input_tokens,
            "output_tokens": rec.output_tokens,
            "cached_tokens": rec.cached_tokens,
            "blocked": 1 if rec.status == RequestStatus.BLOCKED_BY_POLICY else 0,
            "errors": 1 if rec.status in (RequestStatus.ERROR, RequestStatus.INVALID_OUTPUT) else 0,
        }
        increments = {k: F(k) + v for k, v in deltas.items()}
        rows = model_cls.objects.using(alias).filter(org_id=rec.org_id, day=day, model=rec.model)
        if rows.update(**increments):
            return
        try:
            with transaction.atomic(using=alias):
                model_cls(org_id=rec.org_id, day=day, model=rec.model, **deltas).save(
                    using=alias, force_insert=True)
        except IntegrityError:  # concurrent creator won: increment instead
            rows.update(**increments)
