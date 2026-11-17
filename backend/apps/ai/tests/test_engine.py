"""GeminiService engine: structured output + repair, retries, breaker, quota, config, policy, logging.

All Gemini traffic goes to FakeTransport (a network spy): `transport.total_calls` proves "no network call".
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from apps.ai.config import OrgAIConfig
from apps.ai.exceptions import AIInvalidOutput
from apps.ai.policy import evaluate_policy
from apps.ai.quota import ModelLimits
from apps.ai.testing import FakeClock, FakeTransport, MemoryRecorder, build_engine, rate_limited, unavailable
from apps.ai.timeutil import next_pacific_midnight
from apps.ai.transport import RawResult, StreamChunk, Usage
from apps.core.exceptions import AIPolicyBlocked, AIQuotaExhausted, AIUnavailable
from nexora.contracts import PayloadClass, PayloadManifest

SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string", "nullable": True}, "explanation": {"type": "string"},
                   "assumptions": {"type": "array", "items": {"type": "string"}}},
    "required": ["sql", "explanation", "assumptions"],
}
GOOD = {"sql": "SELECT 1", "explanation": "One.", "assumptions": []}
DS = "dataset:0190a0c4-0000-7000-8000-0000000000d1"
PAYLOAD = PayloadManifest({PayloadClass.SCHEMA: (DS,)})
T0 = datetime(2026, 11, 17, 18, 0, 30, tzinfo=UTC).timestamp()


def call(engine, **kw):
    args = {"feature": "nl2sql", "prompt_key": "nl2sql.generate", "system": "sys", "prompt": "question?",
            "schema": SCHEMA, "payload": PAYLOAD}
    args.update(kw)
    return engine.generate_structured(**args)


@pytest.fixture
def setup(org_ctx, gemini_key):
    transport = FakeTransport()
    engine, parts = build_engine(transport=transport, clock=FakeClock(T0))
    return engine, parts


def test_structured_output_ok_and_logged(setup, org_ctx):
    engine, p = setup
    p["transport"].push(FakeTransport.json(GOOD, input_tokens=321, output_tokens=45, cached_tokens=7))
    assert call(engine) == GOOD
    req = p["transport"].calls[0]
    assert req.response_schema == SCHEMA
    assert req.model == "gemini-3.5-flash-lite"
    assert req.temperature == 0.0
    [rec] = p["recorder"].records
    assert rec.status == "ok"
    assert (rec.input_tokens, rec.output_tokens, rec.cached_tokens) == (321, 45, 7)
    assert rec.prompt_key == "nl2sql.generate" and rec.prompt_version == "1"
    assert rec.payload_classes == ["SCHEMA"] and rec.payload_assets == [DS]
    assert rec.org_id == org_ctx[0] and rec.user_id == org_ctx[1]
    assert rec.gemini_tier == "free" and rec.attempts == 1
    assert len(rec.prompt_sha256) == 64
    assert rec.prompt_text == ""  # log_prompt_text is off by default
    # token reconciliation: org token counter == actual tokens
    assert p["limiter"].usage(org_ctx[0]).org_tokens == 321 + 45


def test_reasoning_tier_uses_reasoning_model(setup):
    engine, p = setup
    p["transport"].push(FakeTransport.json(GOOD))
    call(engine, tier="reasoning", prompt_key="nl2sql.repair")
    assert p["transport"].calls[0].model == "gemini-3.8-flash"


def test_invalid_json_triggers_exactly_one_repair(setup):
    engine, p = setup
    p["transport"].push(FakeTransport.text("{not json"), FakeTransport.json(GOOD))
    assert call(engine) == GOOD
    assert len(p["transport"].calls) == 2
    repair_prompt = p["transport"].calls[1].prompt
    assert "did not match the required JSON schema" in repair_prompt
    assert "invalid JSON" in repair_prompt
    assert p["recorder"].statuses == ["invalid_output", "ok"]
    assert p["recorder"].records[1].error_code == "repaired"


def test_schema_violation_repair_then_give_up(setup):
    engine, p = setup
    bad = {"sql": 5, "explanation": "x"}  # wrong type + missing field
    p["transport"].push(FakeTransport.json(bad), FakeTransport.json(bad))
    with pytest.raises(AIInvalidOutput) as exc:
        call(engine)
    assert exc.value.get_codes() == "ai_invalid_output"
    assert len(p["transport"].calls) == 2  # never a second repair
    assert p["recorder"].statuses == ["invalid_output", "invalid_output"]
    assert p["recorder"].records[1].error_code == "repair_failed"


def test_repair_skipped_when_budget_exhausted(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), org_config=OrgAIConfig(daily_request_budget=1))
    p["transport"].push(FakeTransport.text("nope"))
    with pytest.raises(AIInvalidOutput) as exc:
        call(engine)
    assert exc.value.extra["repair"] == "skipped_no_budget"
    assert len(p["transport"].calls) == 1


def test_fenced_json_is_accepted(setup):
    engine, p = setup
    p["transport"].push(FakeTransport.text("```json\n" + json.dumps(GOOD) + "\n```"))
    assert call(engine) == GOOD


def test_safety_block_maps_to_invalid_output_without_repair(setup):
    engine, p = setup
    p["transport"].push(RawResult(text=None, usage=Usage(10, 0, 0), finish_reason="SAFETY"))
    with pytest.raises(AIInvalidOutput) as exc:
        call(engine)
    assert exc.value.extra["reason"] == "safety"
    assert len(p["transport"].calls) == 1
    assert p["recorder"].records[0].error_code == "safety"


def test_429_per_minute_is_retried_honouring_retry_delay(setup):
    engine, p = setup
    p["transport"].push(rate_limited("rate_minute", retry_delay_s=7.0), FakeTransport.json(GOOD))
    assert call(engine) == GOOD
    assert p["sleeps"] == [7.0]  # backoff (~1s) is raised to the server's retryDelay
    assert len(p["transport"].calls) == 2
    assert p["recorder"].records[-1].attempts == 2


def test_503_backoff_with_jitter_then_success(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), rng=lambda: 1.0)
    p["transport"].push(unavailable(), unavailable(), FakeTransport.json(GOOD))
    assert call(engine) == GOOD
    assert p["sleeps"] == [1.25, 2.5]  # 1s, 2s exponential with +25% jitter
    assert p["recorder"].records[-1].attempts == 3


def test_503_max_three_attempts_then_unavailable(setup):
    engine, p = setup
    p["transport"].push(unavailable(), unavailable(), unavailable(), FakeTransport.json(GOOD))
    with pytest.raises(AIUnavailable) as exc:
        call(engine)
    assert exc.value.get_codes() == "ai_unavailable"
    assert len(p["transport"].calls) == 3
    rec = p["recorder"].records[-1]
    assert rec.status == "error" and rec.attempts == 3 and rec.error_code == "gemini_unavailable_503"


def test_429_minute_exhausted_opens_minute_breaker(setup):
    engine, p = setup
    p["transport"].push(*[rate_limited("rate_minute", retry_delay_s=2.0)] * 3)
    with pytest.raises(AIQuotaExhausted) as exc:
        call(engine)
    assert exc.value.get_codes() == "ai_rate_limited"
    assert p["limiter"].breaker("gemini-3.5-flash-lite").state == "open_minute"
    assert p["recorder"].records[-1].status == "rate_limited"


def test_long_retry_delay_is_not_busy_waited(setup):
    engine, p = setup
    p["transport"].push(rate_limited("rate_minute", retry_delay_s=45.0))
    with pytest.raises(AIQuotaExhausted):
        call(engine)
    assert p["sleeps"] == []
    assert len(p["transport"].calls) == 1


def test_429_per_day_opens_breaker_until_pacific_midnight_and_blocks_further_calls(setup):
    engine, p = setup
    p["transport"].push(rate_limited("rate_day", retry_delay_s=3.0))
    with pytest.raises(AIQuotaExhausted) as exc:
        call(engine)
    assert len(p["transport"].calls) == 1  # 429 per-day: never retried
    expected = next_pacific_midnight(T0)
    assert exc.value.extra["resets_at"] == expected.isoformat()
    assert expected == datetime(2026, 11, 18, 8, 0, tzinfo=UTC)
    # subsequent requests are refused by the open breaker WITHOUT touching the network
    with pytest.raises(AIQuotaExhausted) as exc2:
        call(engine)
    assert exc2.value.extra["breaker"] == "open_day"
    assert exc2.value.extra["resets_at"] == expected.isoformat()
    assert len(p["transport"].calls) == 1
    assert p["recorder"].statuses == ["quota_exhausted", "quota_exhausted"]
    # after midnight Pacific it closes again
    p["clock"].now = expected.timestamp() + 1
    p["transport"].push(FakeTransport.json(GOOD))
    assert call(engine) == GOOD


def test_not_configured_raises_without_network(org_ctx, settings):
    settings.GEMINI_API_KEY = ""
    settings.GEMINI_TIER = "free"
    engine, p = build_engine(clock=FakeClock(T0))
    with pytest.raises(AIUnavailable) as exc:
        call(engine)
    assert exc.value.get_codes() == "ai_not_configured"
    assert p["transport"].total_calls == 0
    assert p["recorder"].statuses == ["not_configured"]


def test_policy_block_happens_before_network_and_is_logged(org_ctx, gemini_key):
    def gate(payload):
        from apps.ai.policy import check_policy

        return check_policy(payload, state={"policy": "synthetic_only", "consent_valid": True},
                            classify=lambda ref: "internal", tier="free")

    engine, p = build_engine(clock=FakeClock(T0), policy_check=gate)
    with pytest.raises(AIPolicyBlocked):
        call(engine)
    assert p["transport"].total_calls == 0
    [rec] = p["recorder"].records
    assert rec.status == "blocked_by_policy"
    assert rec.payload_assets == [DS]
    assert rec.attempts == 0
    assert p["limiter"].usage(org_ctx[0]).org_requests == 0  # blocked requests consume no budget


def test_policy_block_even_without_api_key(org_ctx, settings):
    settings.GEMINI_API_KEY = ""

    def gate(payload):
        from apps.ai.policy import check_policy

        return check_policy(payload, state={"policy": "synthetic_only", "consent_valid": True},
                            classify=lambda ref: "confidential", tier="free")

    engine, p = build_engine(clock=FakeClock(T0), policy_check=gate)
    with pytest.raises(AIPolicyBlocked):
        call(engine)
    assert p["recorder"].statuses == ["blocked_by_policy"]


def test_undeclared_payload_class_is_refused(setup):
    engine, p = setup
    values = PayloadManifest({PayloadClass.SCHEMA: (DS,), PayloadClass.VALUES: (DS,)})
    with pytest.raises(AIPolicyBlocked) as exc:
        call(engine, payload=values)  # nl2sql.generate declares SCHEMA only
    assert exc.value.extra["reason"] == "undeclared_payload_class"
    assert p["transport"].total_calls == 0


def test_prompt_is_redacted_before_sending(setup):
    engine, p = setup
    p["transport"].push(FakeTransport.json(GOOD))
    call(engine, prompt="who is ravi.k@example.test with card 4111 1111 1111 1111?")
    sent = p["transport"].calls[0].prompt
    assert "example.test" not in sent and "4111" not in sent
    assert "[EMAIL]" in sent and "[CARD]" in sent
    assert p["recorder"].records[0].extra["redactions"] == {"email": 1, "card": 1}


def test_prompt_text_logged_only_when_enabled_and_redacted(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), org_config=OrgAIConfig(log_prompt_text=True))
    p["transport"].push(FakeTransport.json(GOOD))
    call(engine, prompt="email ravi.k@example.test")
    text = p["recorder"].records[0].prompt_text
    assert "[EMAIL]" in text and "example.test" not in text


def test_api_key_and_prompt_never_in_logs(setup, caplog, gemini_key):
    engine, p = setup
    p["transport"].push(FakeTransport.json(GOOD))
    with caplog.at_level(logging.DEBUG):
        from apps.ai.recorder import emit_log_line

        engine._recorder = MemoryRecorder()
        call(engine, prompt="UNIQUE-PROMPT-MARKER")
        emit_log_line(engine._recorder.records[0])
    assert gemini_key not in caplog.text
    assert "UNIQUE-PROMPT-MARKER" not in caplog.text
    assert '"status": "ok"' in caplog.text


def test_org_budget_exhaustion(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), org_config=OrgAIConfig(daily_request_budget=2),
                             transport=FakeTransport(default=FakeTransport.json(GOOD)))
    call(engine)
    call(engine)
    with pytest.raises(AIQuotaExhausted) as exc:
        call(engine)
    assert exc.value.extra["scope"] == "org_day"
    assert len(p["transport"].calls) == 2
    assert p["recorder"].statuses[-1] == "quota_exhausted"


def test_per_user_cap(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), org_config=OrgAIConfig(per_user_daily_requests=1),
                             transport=FakeTransport(default=FakeTransport.json(GOOD)))
    call(engine)
    with pytest.raises(AIQuotaExhausted) as exc:
        call(engine)
    assert exc.value.extra["scope"] == "user_day"


def test_project_rpd_from_model_quota(org_ctx, gemini_key):
    engine, p = build_engine(clock=FakeClock(T0), model_limits=ModelLimits(rpm=100, tpm=10**7, rpd=2),
                             transport=FakeTransport(default=FakeTransport.json(GOOD)))
    call(engine)  # effective rpd = max(1, floor(2*0.9)) = 1
    with pytest.raises(AIQuotaExhausted) as exc:
        call(engine)
    assert exc.value.extra["scope"] == "project_day"
    assert p["limiter"].breaker("gemini-3.5-flash-lite").state == "open_day"


def test_limiter_outage_fails_closed(setup):
    engine, p = setup
    p["redis"].fail = True
    with pytest.raises(AIUnavailable) as exc:
        call(engine)
    assert exc.value.get_codes() == "ai_limiter_unavailable"
    assert p["transport"].total_calls == 0


def test_request_trace_collects_ids(setup):
    from apps.ai.engine import request_trace

    engine, p = setup
    p["transport"].push(FakeTransport.text("bad"), FakeTransport.json(GOOD))
    with request_trace() as ids:
        call(engine)
    assert len(ids) == 2


def test_generate_text(setup):
    engine, p = setup
    p["transport"].push(FakeTransport.text("Revenue rose."))
    out = engine.generate_text(feature="bi", prompt_key="bi.narrate", system="s", prompt="p",
                               payload=PayloadManifest({PayloadClass.SCHEMA: (DS,), PayloadClass.VALUES: (DS,)}))
    assert out == "Revenue rose."
    assert p["transport"].calls[0].response_schema is None
    assert p["transport"].calls[0].temperature == 0.3  # bi.narrate default


# ------------------------------------------------------------------------------------------ streaming
def _narrate(engine, **kw):
    args = {"feature": "bi.narrate", "prompt_key": "bi.narrate", "system": "s", "prompt": "p",
            "payload": PayloadManifest({PayloadClass.SCHEMA: (DS,), PayloadClass.VALUES: (DS,)})}
    args.update(kw)
    return engine.stream(**args)


def test_stream_yields_and_records_usage_from_final_chunk(setup):
    engine, p = setup
    p["transport"].push([StreamChunk("Revenue "), StreamChunk("rose 12%."),
                         StreamChunk("", usage=Usage(200, 9, 0), finish_reason="STOP")])
    assert "".join(_narrate(engine)) == "Revenue rose 12%."
    [rec] = p["recorder"].records
    assert rec.status == "ok" and rec.input_tokens == 200 and rec.output_tokens == 9


def test_stream_checks_are_eager(org_ctx, gemini_key):
    def gate(payload):
        from apps.ai.policy import check_policy

        return check_policy(payload, state={"policy": "schema_only", "consent_valid": True},
                            classify=lambda ref: "internal", tier="free")

    engine, p = build_engine(clock=FakeClock(T0), policy_check=gate)
    with pytest.raises(AIPolicyBlocked):
        _narrate(engine)  # raised at call time, before iterating
    assert p["transport"].total_calls == 0


def test_stream_retries_before_first_token(setup):
    engine, p = setup
    p["transport"].push(unavailable(), [StreamChunk("ok", usage=Usage(5, 1, 0))])
    assert "".join(_narrate(engine)) == "ok"
    assert p["recorder"].records[-1].attempts == 2


def test_stream_error_after_first_token_is_not_retried(setup):
    engine, p = setup
    p["transport"].push([StreamChunk("partial"), unavailable()])
    it = _narrate(engine)
    assert next(it) == "partial"
    with pytest.raises(AIUnavailable):
        list(it)
    assert len(p["transport"].stream_calls) == 1


def test_stream_client_close_is_recorded(setup):
    engine, p = setup
    p["transport"].push([StreamChunk("a"), StreamChunk("b"), StreamChunk("c")])
    it = _narrate(engine)
    next(it)
    it.close()
    assert p["recorder"].records[-1].error_code == "client_closed"


def test_stream_safety_block(setup):
    engine, p = setup
    p["transport"].push([StreamChunk("", finish_reason="SAFETY")])
    with pytest.raises(AIInvalidOutput):
        list(_narrate(engine))
    assert p["recorder"].records[-1].error_code == "safety"


def test_allow_all_policy_helper_is_test_only():
    d = evaluate_policy(PAYLOAD, state={"policy": "synthetic_only", "consent_valid": True},
                        classify=lambda r: "synthetic", tier="free")
    assert d.allowed
