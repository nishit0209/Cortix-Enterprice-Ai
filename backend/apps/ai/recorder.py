"""Persistence of request metadata: AIRequestLog (append-only) + AIUsageDaily counters + a structured log line."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Protocol
from uuid import UUID

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F

from .constants import RequestStatus
from .timeutil import pacific_day

log = logging.getLogger("nexora.ai")


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


class DbRecorder:
    """Writes inside the caller's tenant transaction (savepoint, never poisons the outer transaction)."""

    def record(self, rec: RequestRecord) -> UUID | None:
        from .models import AIRequestLog, AIUsageDaily

        emit_log_line(rec)
        try:
            with transaction.atomic():
                row = AIRequestLog.objects.create(
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
                self._bump_usage(AIUsageDaily, rec)
            return row.id
        except DatabaseError:
            log.exception("ai: failed to persist AIRequestLog (status=%s feature=%s)", rec.status, rec.feature)
            return None

    @staticmethod
    def _bump_usage(model_cls, rec: RequestRecord) -> None:
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
        updated = model_cls.objects.filter(day=day, model=rec.model).update(
            **{k: F(k) + v for k, v in deltas.items()})
        if updated:
            return
        try:
            with transaction.atomic():
                model_cls.objects.create(org_id=rec.org_id, day=day, model=rec.model, **deltas)
        except IntegrityError:  # concurrent creator won: increment instead
            model_cls.objects.filter(day=day, model=rec.model).update(**{k: F(k) + v for k, v in deltas.items()})
