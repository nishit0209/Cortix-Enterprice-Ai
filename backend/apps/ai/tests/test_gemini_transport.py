"""SDK adapter (apps.ai.gemini) — exercised with a stub client object; no network, no real key."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.ai.gemini import GenaiTransport, classify_api_error, parse_retry_delay
from apps.ai.transport import GenerateRequest

REQ = GenerateRequest(model="gemini-3.5-flash-lite", system="SYS", prompt="PROMPT", temperature=0.0,
                      max_output_tokens=512, timeout_s=60, response_schema={"type": "object", "properties": {}})


def _quota_details(quota_id: str, delay: str | None = "13s"):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                "quotaId": quota_id}]}]
    if delay:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return details


def test_classify_429_per_minute_with_retry_delay():
    err = classify_api_error(429, "RESOURCE_EXHAUSTED", _quota_details("GenerateRequestsPerMinutePerProjectPerModel-FreeTier"))
    assert err.kind == "rate_minute" and err.retry_delay_s == 13.0 and err.retryable


def test_classify_429_per_day():
    err = classify_api_error(429, "RESOURCE_EXHAUSTED", _quota_details("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    assert err.kind == "rate_day" and not err.retryable


@pytest.mark.parametrize(("code", "status", "kind"), [
    (503, "UNAVAILABLE", "unavailable"), (500, "INTERNAL", "unavailable"), (504, "DEADLINE_EXCEEDED", "timeout"),
    (401, "UNAUTHENTICATED", "auth"), (403, "PERMISSION_DENIED", "auth"), (400, "INVALID_ARGUMENT", "bad_request"),
    (404, "NOT_FOUND", "bad_request"), (418, None, "unknown"),
])
def test_classify_other_errors(code, status, kind):
    assert classify_api_error(code, status, None).kind == kind


@pytest.mark.parametrize(("value", "expected"), [("13s", 13.0), ("1.5s", 1.5), ({"seconds": 2, "nanos": 500000000}, 2.5),
                                                 (7, 7.0), ("soon", None), (None, None)])
def test_parse_retry_delay(value, expected):
    assert parse_retry_delay(value) == expected


def test_translate_real_sdk_api_error():
    from google.genai import errors

    body = {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED",
                      "details": _quota_details("GenerateRequestsPerDayPerProjectPerModel-FreeTier", None)}}
    exc = errors.ClientError(429, body)
    err = GenaiTransport._translate(exc)
    assert err.kind == "rate_day"
    exc2 = errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
    assert GenaiTransport._translate(exc2).kind == "unavailable"


class _StubModels:
    def __init__(self, response=None, error=None, chunks=None):
        self.response, self.error, self.chunks = response, error, chunks
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if self.error:
            raise self.error
        return self.response

    def generate_content_stream(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        yield from self.chunks


def _resp(text, prompt=11, cand=7, cached=3, thoughts=2, finish="STOP"):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand,
                                       cached_content_token_count=cached, thoughts_token_count=thoughts),
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(value=finish))],
        prompt_feedback=None,
    )


def test_generate_builds_json_mode_config_and_extracts_usage():
    t = GenaiTransport()
    models = _StubModels(response=_resp('{"a": 1}'))
    t._client = SimpleNamespace(models=models)
    out = t.generate(REQ)
    assert out.text == '{"a": 1}'
    assert (out.usage.input_tokens, out.usage.output_tokens, out.usage.cached_tokens) == (11, 9, 3)
    assert out.finish_reason == "STOP" and not out.safety_blocked
    model, contents, config = models.calls[0]
    assert model == "gemini-3.5-flash-lite" and contents == "PROMPT"
    assert config.response_mime_type == "application/json"
    assert config.response_schema == REQ.response_schema
    assert config.system_instruction == "SYS"
    assert config.temperature == 0.0 and config.max_output_tokens == 512
    assert config.http_options.timeout == 60_000


def test_generate_without_schema_is_plain_text():
    t = GenaiTransport()
    models = _StubModels(response=_resp("hello"))
    t._client = SimpleNamespace(models=models)
    req = GenerateRequest(model="m", system="", prompt="p", temperature=0.3, max_output_tokens=10, timeout_s=5)
    t.generate(req)
    config = models.calls[0][2]
    assert config.response_mime_type is None and config.response_schema is None


def test_generate_translates_sdk_errors():
    from google.genai import errors

    from apps.ai.transport import TransportError

    t = GenaiTransport()
    t._client = SimpleNamespace(models=_StubModels(error=errors.ServerError(
        503, {"error": {"code": 503, "message": "x", "status": "UNAVAILABLE"}})))
    with pytest.raises(TransportError) as exc:
        t.generate(REQ)
    assert exc.value.kind == "unavailable"


def test_safety_block_detected():
    t = GenaiTransport()
    blocked = SimpleNamespace(text=None, usage_metadata=None, candidates=[],
                              prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(value="SAFETY")))
    t._client = SimpleNamespace(models=_StubModels(response=blocked))
    assert t.generate(REQ).safety_blocked


def test_stream_chunks():
    t = GenaiTransport()
    chunks = [SimpleNamespace(text="a", usage_metadata=None, candidates=[], prompt_feedback=None),
              _resp("b", prompt=4, cand=2, cached=0, thoughts=0)]
    t._client = SimpleNamespace(models=_StubModels(chunks=chunks))
    out = list(t.stream(REQ))
    assert [c.text for c in out] == ["a", "b"]
    assert out[-1].usage.input_tokens == 4


def test_client_requires_key(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.GEMINI_API_KEY = ""
    settings.GEMINI_TIER = "free"
    with pytest.raises(ImproperlyConfigured):
        GenaiTransport()._get_client()


def test_client_creation_does_not_call_network(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    settings.GEMINI_TIER = "free"
    client = GenaiTransport()._get_client()  # constructing the client performs no request
    assert client is not None
