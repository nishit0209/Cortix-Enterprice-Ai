"""GeminiService implementation (contract: nexora.contracts.GeminiService).

Every request goes through, in order:
  1. AIDataPolicy gate (+ the prompt's declared payload classes)  -> AIPolicyBlocked, NO network call
  2. configuration check (GEMINI_API_KEY / Vertex settings)         -> AIUnavailable(ai_not_configured)
  3. PII redactor on system + prompt
  4. circuit breaker                                                 -> AIQuotaExhausted(resets_at)
  5. quota limiter (project RPM/TPM/RPD, org budget, user cap)        -> AIQuotaExhausted
  6. transport call with retries (429 per-minute / 503 / timeouts; backoff + jitter, honouring
     RetryInfo.retryDelay, max 3 attempts). 429 per-day opens the breaker until midnight Pacific.
  7. structured output: JSON parse + schema validation, ONE repair call, then AIInvalidOutput
  8. AIRequestLog + AIUsageDaily + structured log line (never the API key; prompt text only when the
     org enabled log_prompt_text, and then only the redacted text).
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from django.core.exceptions import ImproperlyConfigured

from apps.core.exceptions import AIPolicyBlocked, AIQuotaExhausted, AIUnavailable
from apps.core.middleware import request_id_var
from apps.core.tenancy.context import current_actor, require_org_id
from nexora.contracts import PayloadManifest

from . import config as cfgmod
from .constants import (
    CODE_INVALID_OUTPUT,
    CODE_LIMITER_UNAVAILABLE,
    CODE_NOT_CONFIGURED,
    CODE_POLICY_BLOCKED,
    CODE_QUOTA_EXHAUSTED,
    CODE_RATE_LIMITED,
    CODE_UNAVAILABLE,
    Priority,
    RequestStatus,
)
from .exceptions import AIInvalidOutput
from .policy import PolicyDecision, check_policy
from .prompts import data_block, find_prompt
from .quota import Denial, LimiterUnavailable, QuotaDenied, QuotaLimiter, Reservation
from .recorder import DbRecorder, Recorder, RequestRecord
from .redaction import redact
from .schema import OutputParseError, parse_json_output, validate
from .transport import GenerateRequest, RawResult, Transport, TransportError, Usage

log = logging.getLogger("nexora.ai")

_trace: ContextVar[list[UUID] | None] = ContextVar("ai_request_trace", default=None)


@contextmanager
def request_trace():
    """Collect the AIRequestLog ids written inside the block (e.g. for SqlProposal.ai_request_ids)."""
    token = _trace.set([])
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


def estimate_tokens(*texts: str) -> int:
    """Local heuristic (chars / 3.5) with a 10% safety margin. Never calls count_tokens (it costs a request)."""
    chars = sum(len(t or "") for t in texts)
    return int(math.ceil(chars / 3.5 * 1.1))


@dataclass
class _Call:
    org_id: UUID
    user_id: UUID | None
    feature: str
    prompt_key: str
    prompt_version: str
    model: str
    tier: str
    priority: str
    gemini_tier: str
    decision: PolicyDecision | None
    system: str = ""
    prompt: str = ""
    sha256: str = ""
    log_prompt_text: bool = False
    redactions: dict = field(default_factory=dict)
    temperature: float = 0.0
    max_output_tokens: int = 2048

    def record(self, status: str, **kw) -> RequestRecord:
        d = self.decision
        return RequestRecord(
            org_id=self.org_id,
            user_id=self.user_id,
            feature=self.feature,
            model=self.model,
            prompt_key=self.prompt_key,
            prompt_version=self.prompt_version,
            payload_classes=list(d.payload_classes) if d else kw.pop("payload_classes", []),
            payload_assets=list(d.asset_refs) if d else kw.pop("payload_assets", []),
            ai_data_policy=(d.effective_policy if d else kw.pop("ai_data_policy", "synthetic_only")),
            gemini_tier=self.gemini_tier,
            priority=self.priority,
            status=status,
            prompt_sha256=self.sha256,
            prompt_text=(f"{self.system}\n\n{self.prompt}"[:20000] if self.log_prompt_text else ""),
            request_id=request_id_var.get(),
            extra={"redactions": self.redactions, **kw.pop("extra", {})},
            **kw,
        )


class _CallFailed(Exception):
    def __init__(self, status: str, error_code: str, exc: Exception, attempts: int, latency_ms: int,
                 usage: Usage | None = None):
        super().__init__(error_code)
        self.status = status
        self.error_code = error_code
        self.exc = exc
        self.attempts = attempts
        self.latency_ms = latency_ms
        self.usage = usage or Usage()


def _default_limiter() -> QuotaLimiter:
    from apps.core.jobs import redis_client

    return QuotaLimiter(redis_client)


def _default_transport() -> Transport:
    from .gemini import GenaiTransport

    return GenaiTransport()


def _actor_user() -> UUID | None:
    kind, actor_id = current_actor.get()
    return actor_id if kind == "user" and actor_id else None


class GeminiEngine:
    """Thread-safe (no per-request state on the instance)."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], Transport] | None = None,
        limiter: QuotaLimiter | None = None,
        recorder: Recorder | None = None,
        org_config_loader: Callable[[UUID], cfgmod.OrgAIConfig] | None = None,
        model_limits_loader: Callable[[str], object] | None = None,
        policy_check: Callable[[PayloadManifest], PolicyDecision] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        max_attempts: int = 3,
        max_retry_wait_s: float = 20.0,
    ):
        self._transport_factory = transport_factory or _default_transport
        self._transport: Transport | None = None
        self._limiter = limiter
        self._recorder = recorder or DbRecorder()
        self._org_config_loader = org_config_loader or cfgmod.load_org_config
        self._model_limits_loader = model_limits_loader or cfgmod.load_model_limits
        self._policy_check = policy_check or check_policy
        self._sleep = sleep
        self._rng = rng
        self.max_attempts = max(1, int(max_attempts))
        self.max_retry_wait_s = float(max_retry_wait_s)

    # ---------------------------------------------------------------------------------- plumbing
    @property
    def limiter(self) -> QuotaLimiter:
        if self._limiter is None:
            self._limiter = _default_limiter()
        return self._limiter

    def _get_transport(self) -> Transport:
        if self._transport is None:
            self._transport = self._transport_factory()
        return self._transport

    def _record(self, rec: RequestRecord) -> UUID | None:
        rid = self._recorder.record(rec)
        trace = _trace.get()
        if trace is not None and rid is not None:
            trace.append(rid)
        return rid

    # ---------------------------------------------------------------------------------- preparation
    def _prepare(self, *, feature: str, prompt_key: str, system: str, prompt: str, payload: PayloadManifest,
                 tier: str, priority: str, user_id: UUID | None, temperature: float | None,
                 max_output_tokens: int | None, expected_output_tokens: int) -> tuple[_Call, Reservation]:
        org_id = require_org_id()
        spec = find_prompt(prompt_key)
        priority = Priority(priority).value
        call = _Call(
            org_id=org_id,
            user_id=user_id or _actor_user(),
            feature=feature,
            prompt_key=prompt_key,
            prompt_version=spec.version if spec else "adhoc",
            model=cfgmod.model_for_tier(tier),
            tier=tier,
            priority=priority,
            gemini_tier=cfgmod.gemini_tier(),
            decision=None,
            temperature=float(temperature if temperature is not None else (spec.temperature if spec else 0.0)),
            max_output_tokens=int(max_output_tokens or (spec.max_output_tokens if spec and spec.max_output_tokens
                                                        else cfgmod.max_output_tokens())),
        )
        declared = {c.value if hasattr(c, "value") else str(c) for c in (spec.payload_classes if spec else ())}

        # 1. policy gate (BEFORE anything touches the network)
        try:
            decision = self._policy_check(payload)
            undeclared = [c for c in decision.payload_classes if spec is not None and c not in declared]
            if undeclared:
                raise AIPolicyBlocked(
                    f"Prompt '{prompt_key}' does not declare payload class(es) {', '.join(undeclared)}.",
                    code=CODE_POLICY_BLOCKED,
                    extra={"reason": "undeclared_payload_class", "payload_classes": undeclared},
                )
            call.decision = decision
        except AIPolicyBlocked as exc:
            extra = getattr(exc, "extra", {}) or {}
            refs = []
            for cls_refs in (getattr(payload, "items", {}) or {}).values():
                refs.extend([cls_refs] if isinstance(cls_refs, str) else list(cls_refs or ()))
            self._record(call.record(
                RequestStatus.BLOCKED_BY_POLICY.value,
                error_code=str(extra.get("reason") or CODE_POLICY_BLOCKED)[:60],
                payload_classes=[getattr(k, "value", str(k)) for k in (getattr(payload, "items", {}) or {})],
                payload_assets=[str(r) for r in refs],
                ai_data_policy=str(extra.get("effective_policy") or "synthetic_only"),
            ))
            raise

        # 2. configuration — never fall back to anything else
        if not cfgmod.is_configured():
            self._record(call.record(RequestStatus.NOT_CONFIGURED.value, error_code=CODE_NOT_CONFIGURED))
            raise AIUnavailable("AI is not configured on this server (no Gemini credentials).",
                                code=CODE_NOT_CONFIGURED)

        # 3. redaction
        rs, rp = redact(system or ""), redact(prompt or "")
        call.system, call.prompt = rs.text, rp.text
        call.redactions = {k: rs.counts.get(k, 0) + rp.counts.get(k, 0) for k in {*rs.counts, *rp.counts}}
        call.sha256 = hashlib.sha256(f"{call.system}\n\n{call.prompt}".encode()).hexdigest()

        org_cfg = self._org_config_loader(org_id)
        call.log_prompt_text = bool(org_cfg.log_prompt_text)

        # 4. breaker + 5. limiter
        reservation = self._admit(call, org_cfg, estimate_tokens(call.system, call.prompt) + expected_output_tokens)
        return call, reservation

    def _admit(self, call: _Call, org_cfg: cfgmod.OrgAIConfig, est_tokens: int) -> Reservation:
        try:
            br = self.limiter.breaker(call.model)
            if br.state != "closed":
                status = (RequestStatus.QUOTA_EXHAUSTED if br.state == "open_day" else RequestStatus.RATE_LIMITED)
                code = CODE_QUOTA_EXHAUSTED if br.state == "open_day" else CODE_RATE_LIMITED
                self._record(call.record(status.value, error_code=f"breaker_{br.state}"))
                raise AIQuotaExhausted(
                    "The AI quota for today is used up." if br.state == "open_day"
                    else "AI is rate limited right now. Please retry shortly.",
                    code=code,
                    extra={"scope": "breaker", "breaker": br.state,
                           "resets_at": br.resets_at.isoformat() if br.resets_at else None},
                )
            return self.limiter.reserve(
                org_id=call.org_id, user_id=call.user_id, model=call.model, priority=call.priority,
                est_tokens=est_tokens, model_limits=self._model_limits_loader(call.model),
                org_limits=org_cfg.limits(),
            )
        except QuotaDenied as denied:
            d = denied.denial
            self._record(call.record(d.status, error_code=d.scope))
            raise self._quota_exc(d) from None
        except LimiterUnavailable as exc:
            self._record(call.record(RequestStatus.ERROR.value, error_code=CODE_LIMITER_UNAVAILABLE))
            raise AIUnavailable("AI is temporarily unavailable (quota service unreachable).",
                                code=CODE_LIMITER_UNAVAILABLE) from exc

    @staticmethod
    def _quota_exc(d: Denial) -> AIQuotaExhausted:
        messages = {
            "project_minute": "AI is busy right now. Please retry in a minute.",
            "project_day": "The platform's AI quota for today is used up.",
            "project_day_priority": "The AI quota reserved for this kind of task is used up for today.",
            "org_day": "Your organization's AI budget for today is used.",
            "org_tokens": "Your organization's AI token budget for today is used.",
            "user_day": "You have reached your personal AI request limit for today.",
        }
        return AIQuotaExhausted(messages.get(d.scope, "The AI quota is exhausted."), code=d.code,
                                extra={"scope": d.scope, "resets_at": d.resets_at.isoformat(),
                                       "retry_after_s": d.retry_after_s})

    # ---------------------------------------------------------------------------------- execution
    def _request(self, call: _Call, schema: dict | None, prompt: str | None = None) -> GenerateRequest:
        return GenerateRequest(
            model=call.model, system=call.system, prompt=prompt if prompt is not None else call.prompt,
            temperature=call.temperature, max_output_tokens=call.max_output_tokens,
            timeout_s=cfgmod.request_timeout_s(), response_schema=schema,
        )

    def _backoff(self, attempt: int, retry_delay: float | None) -> float:
        base = min(8.0, 2.0 ** (attempt - 1))
        jittered = base * (1.0 + 0.25 * self._rng())
        return max(jittered, float(retry_delay or 0.0))

    def _fail_from_transport(self, call: _Call, err: TransportError, attempts: int, latency_ms: int) -> _CallFailed:
        if err.kind == "rate_day":
            br = self.limiter.open_day(call.model)
            exc = AIQuotaExhausted("The platform's AI quota for today is used up.", code=CODE_QUOTA_EXHAUSTED,
                                   extra={"scope": "project_day", "breaker": br.state,
                                          "resets_at": br.resets_at.isoformat() if br.resets_at else None})
            return _CallFailed(RequestStatus.QUOTA_EXHAUSTED.value, "gemini_429_day", exc, attempts, latency_ms)
        if err.kind == "rate_minute":
            br = self.limiter.open_minute(call.model, err.retry_delay_s or 60.0)
            exc = AIQuotaExhausted("AI is busy right now. Please retry shortly.", code=CODE_RATE_LIMITED,
                                   extra={"scope": "project_minute", "breaker": br.state,
                                          "resets_at": br.resets_at.isoformat() if br.resets_at else None})
            return _CallFailed(RequestStatus.RATE_LIMITED.value, "gemini_429_minute", exc, attempts, latency_ms)
        code = f"gemini_{err.kind}" + (f"_{err.http_status}" if err.http_status else "")
        exc = AIUnavailable("AI is temporarily unavailable. Please retry.", code=CODE_UNAVAILABLE,
                            extra={"reason": err.kind})
        return _CallFailed(RequestStatus.ERROR.value, code, exc, attempts, latency_ms)

    def _retry_admit(self, call: _Call) -> None:
        """Count a retry attempt against the PROJECT counters only (the org budget was charged once)."""
        try:
            self.limiter.reserve(org_id=call.org_id, user_id=None, model=call.model, priority=call.priority,
                                 est_tokens=0, model_limits=self._model_limits_loader(call.model), org_limits=None)
        except QuotaDenied as denied:
            raise _CallFailed(denied.denial.status, denied.denial.scope, self._quota_exc(denied.denial), 0, 0) from None
        except LimiterUnavailable as exc:
            raise _CallFailed(RequestStatus.ERROR.value, CODE_LIMITER_UNAVAILABLE,
                              AIUnavailable("AI is temporarily unavailable.", code=CODE_LIMITER_UNAVAILABLE),
                              0, 0) from exc

    def _execute(self, call: _Call, req: GenerateRequest) -> tuple[RawResult, int, int]:
        attempts = 0
        started = time.monotonic()
        while True:
            attempts += 1
            try:
                transport = self._get_transport()
                result = transport.generate(req)
                return result, attempts, int((time.monotonic() - started) * 1000)
            except ImproperlyConfigured as exc:
                raise _CallFailed(RequestStatus.NOT_CONFIGURED.value, CODE_NOT_CONFIGURED,
                                  AIUnavailable(str(exc), code=CODE_NOT_CONFIGURED), 0, 0) from exc
            except TransportError as err:
                latency = int((time.monotonic() - started) * 1000)
                if err.retryable and attempts < self.max_attempts:
                    delay = self._backoff(attempts, err.retry_delay_s)
                    if delay <= self.max_retry_wait_s:
                        log.warning("ai: %s on %s (attempt %d), retrying in %.1fs",
                                    err.kind, call.model, attempts, delay)
                        self._sleep(delay)
                        try:
                            self._retry_admit(call)
                        except _CallFailed as failed:
                            failed.attempts, failed.latency_ms = attempts, latency
                            raise
                        continue
                raise self._fail_from_transport(call, err, attempts, latency) from err

    def _finish_failure(self, call: _Call, failed: _CallFailed, reservation: Reservation | None):
        self._record(call.record(failed.status, error_code=failed.error_code, attempts=failed.attempts,
                                 latency_ms=failed.latency_ms, input_tokens=failed.usage.input_tokens,
                                 output_tokens=failed.usage.output_tokens))
        if reservation is not None:
            self.limiter.reconcile(reservation, failed.usage.total)
        raise failed.exc

    # ---------------------------------------------------------------------------------- public API
    def generate_structured(self, *, feature: str, prompt_key: str, system: str, prompt: str, schema: dict,
                            payload: PayloadManifest, tier: str = "fast", priority: str = Priority.INTERACTIVE,
                            user_id: UUID | None = None, temperature: float | None = None,
                            max_output_tokens: int | None = None) -> dict:
        call, reservation = self._prepare(
            feature=feature, prompt_key=prompt_key, system=system, prompt=prompt, payload=payload, tier=tier,
            priority=priority, user_id=user_id, temperature=temperature, max_output_tokens=max_output_tokens,
            expected_output_tokens=512,
        )
        try:
            result, attempts, latency = self._execute(call, self._request(call, schema))
        except _CallFailed as failed:
            self._finish_failure(call, failed, reservation)
        self.limiter.reconcile(reservation, result.usage.total)

        data, errors = self._check_output(result, schema)
        if result.safety_blocked:
            self._record(call.record(RequestStatus.INVALID_OUTPUT.value, error_code="safety", attempts=attempts,
                                     latency_ms=latency, **self._usage_kw(result.usage)))
            raise AIInvalidOutput("The AI response was blocked by safety filters.", code=CODE_INVALID_OUTPUT,
                                  extra={"reason": "safety"})
        if not errors:
            self._record(call.record(RequestStatus.OK.value, attempts=attempts, latency_ms=latency,
                                     **self._usage_kw(result.usage)))
            return data
        self._record(call.record(RequestStatus.INVALID_OUTPUT.value, error_code="schema_validation",
                                 attempts=attempts, latency_ms=latency, **self._usage_kw(result.usage),
                                 extra={"validation_errors": errors[:10]}))
        return self._repair(call, schema, result.text or "", errors)

    def _repair(self, call: _Call, schema: dict, previous: str, errors: list[str]) -> dict:
        """Exactly ONE repair call with the validation errors, only if budget remains."""
        repair_prompt = "\n\n".join([
            call.prompt,
            "Your previous response did not match the required JSON schema.",
            data_block("validation_errors", "\n".join(f"- {e}" for e in errors[:10])),
            data_block("previous_response", redact(previous[:4000]).text),
            "Return ONLY a corrected JSON object that matches the schema.",
        ])
        repair_call = _Call(**{**call.__dict__, "prompt": repair_prompt,
                               "feature": call.feature, "prompt_key": call.prompt_key})
        repair_call.sha256 = hashlib.sha256(f"{call.system}\n\n{repair_prompt}".encode()).hexdigest()
        org_cfg = self._org_config_loader(call.org_id)
        try:
            reservation = self._admit(repair_call, org_cfg, estimate_tokens(call.system, repair_prompt) + 512)
        except (AIQuotaExhausted, AIUnavailable):
            raise AIInvalidOutput(code=CODE_INVALID_OUTPUT,
                                  extra={"reason": "schema_validation", "repair": "skipped_no_budget"}) from None
        try:
            result, attempts, latency = self._execute(repair_call, self._request(repair_call, schema))
        except _CallFailed as failed:
            self._finish_failure(repair_call, failed, reservation)
        self.limiter.reconcile(reservation, result.usage.total)
        data, errors2 = self._check_output(result, schema)
        if result.safety_blocked or errors2:
            self._record(repair_call.record(RequestStatus.INVALID_OUTPUT.value,
                                            error_code="safety" if result.safety_blocked else "repair_failed",
                                            attempts=attempts, latency_ms=latency, **self._usage_kw(result.usage),
                                            extra={"validation_errors": errors2[:10], "repair": True}))
            raise AIInvalidOutput(code=CODE_INVALID_OUTPUT,
                                  extra={"reason": "safety" if result.safety_blocked else "schema_validation"})
        self._record(repair_call.record(RequestStatus.OK.value, error_code="repaired", attempts=attempts,
                                        latency_ms=latency, **self._usage_kw(result.usage),
                                        extra={"repair": True}))
        return data

    @staticmethod
    def _check_output(result: RawResult, schema: dict | None):
        if schema is None:
            return result.text, ([] if result.text else ["empty output"])
        try:
            data = parse_json_output(result.text)
        except OutputParseError as exc:
            return None, [str(exc)]
        return data, validate(data, schema)

    @staticmethod
    def _usage_kw(usage: Usage) -> dict:
        return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cached_tokens": usage.cached_tokens}

    def generate_text(self, *, feature: str, prompt_key: str, system: str, prompt: str, payload: PayloadManifest,
                      tier: str = "fast", priority: str = Priority.INTERACTIVE, user_id: UUID | None = None,
                      temperature: float | None = None, max_output_tokens: int | None = None) -> str:
        """Plain-text generation (no schema)."""
        call, reservation = self._prepare(
            feature=feature, prompt_key=prompt_key, system=system, prompt=prompt, payload=payload, tier=tier,
            priority=priority, user_id=user_id, temperature=temperature, max_output_tokens=max_output_tokens,
            expected_output_tokens=256,
        )
        try:
            result, attempts, latency = self._execute(call, self._request(call, None))
        except _CallFailed as failed:
            self._finish_failure(call, failed, reservation)
        self.limiter.reconcile(reservation, result.usage.total)
        if result.safety_blocked or not result.text:
            self._record(call.record(RequestStatus.INVALID_OUTPUT.value,
                                     error_code="safety" if result.safety_blocked else "empty_output",
                                     attempts=attempts, latency_ms=latency, **self._usage_kw(result.usage)))
            raise AIInvalidOutput(code=CODE_INVALID_OUTPUT,
                                  extra={"reason": "safety" if result.safety_blocked else "empty_output"})
        self._record(call.record(RequestStatus.OK.value, attempts=attempts, latency_ms=latency,
                                 **self._usage_kw(result.usage)))
        return result.text

    def stream(self, *, feature: str, prompt_key: str, system: str, prompt: str, payload: PayloadManifest,
               tier: str = "fast", priority: str = Priority.INTERACTIVE, user_id: UUID | None = None,
               temperature: float | None = None, max_output_tokens: int | None = None) -> Iterator[str]:
        """Checks (policy, config, breaker, quota) run EAGERLY; the returned iterator yields text deltas."""
        call, reservation = self._prepare(
            feature=feature, prompt_key=prompt_key, system=system, prompt=prompt, payload=payload, tier=tier,
            priority=priority, user_id=user_id, temperature=temperature, max_output_tokens=max_output_tokens,
            expected_output_tokens=256,
        )
        return self._stream_iter(call, reservation)

    def _stream_iter(self, call: _Call, reservation: Reservation) -> Iterator[str]:
        req = self._request(call, None)
        attempts = 0
        started = time.monotonic()
        usage = Usage()
        yielded = False
        finished = False
        try:
            while True:
                attempts += 1
                try:
                    for chunk in self._get_transport().stream(req):
                        if chunk.usage is not None:
                            usage = chunk.usage
                        blocked = chunk.block_reason or (chunk.finish_reason or "").upper() in {
                            "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
                        if blocked:
                            finished = True
                            self._record(call.record(RequestStatus.INVALID_OUTPUT.value, error_code="safety",
                                                     attempts=attempts, **self._usage_kw(usage),
                                                     latency_ms=int((time.monotonic() - started) * 1000)))
                            self.limiter.reconcile(reservation, usage.total)
                            raise AIInvalidOutput("The AI response was blocked by safety filters.",
                                                  code=CODE_INVALID_OUTPUT, extra={"reason": "safety"})
                        if chunk.text:
                            yielded = True
                            yield chunk.text
                    break
                except ImproperlyConfigured as exc:
                    finished = True
                    self._record(call.record(RequestStatus.NOT_CONFIGURED.value, error_code=CODE_NOT_CONFIGURED))
                    raise AIUnavailable(str(exc), code=CODE_NOT_CONFIGURED) from exc
                except TransportError as err:
                    latency = int((time.monotonic() - started) * 1000)
                    if not yielded and err.retryable and attempts < self.max_attempts:
                        delay = self._backoff(attempts, err.retry_delay_s)
                        if delay <= self.max_retry_wait_s:
                            self._sleep(delay)
                            try:
                                self._retry_admit(call)
                            except _CallFailed as failed:
                                finished = True
                                failed.attempts, failed.latency_ms = attempts, latency
                                self._finish_failure(call, failed, reservation)
                            continue
                    finished = True
                    self._finish_failure(call, self._fail_from_transport(call, err, attempts, latency), reservation)
            finished = True
            self._record(call.record(RequestStatus.OK.value, attempts=attempts, **self._usage_kw(usage),
                                     latency_ms=int((time.monotonic() - started) * 1000)))
            self.limiter.reconcile(reservation, usage.total)
        finally:
            if not finished:  # consumer closed the stream early
                self._record(call.record(RequestStatus.OK.value, error_code="client_closed", attempts=attempts,
                                         **self._usage_kw(usage),
                                         latency_ms=int((time.monotonic() - started) * 1000)))
                self.limiter.reconcile(reservation, usage.total)
