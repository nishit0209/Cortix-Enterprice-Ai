"""Self-tests of the shared AI test doubles (testing.fakes). Golden NL->SQL / RAG eval sets land here in W3+.

Recorded cassettes (AI/ML agent records, QA stores) go to backend/tests/ai_eval/cassettes/.
"""

from __future__ import annotations

import socket

import pytest

from nexora.contracts import GeminiService, PayloadClass, PayloadManifest
from testing.fakes import FakeClock, FakeGeminiError, FakeGeminiService, NetworkSpy

PAYLOAD = PayloadManifest(items={PayloadClass.SCHEMA: ("dataset:018f0000-0000-7000-8000-000000000001",)})


def _call(fake, key="nl2sql.generate", prompt="How much revenue in North?"):
    return fake.generate_structured(
        feature="nl2sql", prompt_key=key, system="sys", prompt=prompt, schema={}, payload=PAYLOAD
    )


def test_fake_satisfies_the_gemini_contract():
    fake: GeminiService = FakeGeminiService()
    assert callable(fake.generate_structured) and callable(fake.stream)


def test_scripted_responses_are_matched_and_copied():
    fake = FakeGeminiService().respond("nl2sql.generate", {"sql": "SELECT 1"})
    out = _call(fake)
    out["sql"] = "mutated"
    assert _call(fake) == {"sql": "SELECT 1"}  # last response repeats, callers cannot mutate the script
    assert fake.call_count == 2 and fake.calls[0].payload is PAYLOAD


def test_responses_are_consumed_in_order_then_repeat():
    fake = FakeGeminiService().respond("k", {"n": 1}).respond("k", {"n": 2})
    assert [_call(fake, "k")["n"] for _ in range(3)] == [1, 2, 2]


def test_feature_and_default_fallbacks_and_callables():
    fake = (
        FakeGeminiService().respond("nl2sql", {"by": "feature"}).respond("*", lambda prompt: {"echo": prompt})
    )
    assert _call(fake, "unknown.key")["by"] == "feature"
    other = fake.generate_structured(
        feature="x", prompt_key="y", system="", prompt="hi", schema={}, payload=PAYLOAD
    )
    assert other == {"echo": "hi"}


def test_unscripted_calls_fail_loudly_and_errors_can_be_injected():
    fake = FakeGeminiService()
    with pytest.raises(FakeGeminiError):
        _call(fake)
    fake.respond("k", {"ok": True}).fail("k", TimeoutError("boom"))
    with pytest.raises(TimeoutError):
        _call(fake, "k")
    assert _call(fake, "k") == {"ok": True}


def test_stream_and_prompt_assertions():
    fake = FakeGeminiService().respond("narrate", stream=["Revenue ", "grew 4%"])
    text = "".join(
        fake.stream(feature="bi", prompt_key="narrate", system="s", prompt="SECRET-CANARY", payload=PAYLOAD)
    )
    assert text == "Revenue grew 4%"
    with pytest.raises(AssertionError):
        fake.assert_not_in_prompts("SECRET-CANARY")
    with pytest.raises(AssertionError):
        fake.assert_no_calls()


def test_network_spy_blocks_external_hosts_and_allows_loopback():
    with NetworkSpy() as spy:
        with pytest.raises(OSError):
            socket.create_connection(("generativelanguage.googleapis.com", 443), timeout=1)
        with pytest.raises(OSError):
            socket.create_connection(("203.0.113.10", 443), timeout=1)  # TEST-NET-3
    assert ("generativelanguage.googleapis.com", 443) in spy.attempts
    assert ("203.0.113.10", 443) in spy.external
    assert socket.create_connection is not None and spy.block


def test_fake_gemini_fixture_patches_the_service(fake_gemini):
    from apps.ai import services

    assert services.gemini() is fake_gemini


def test_fake_clock(fake_clock):
    from django.utils import timezone

    start = timezone.now()
    fake_clock.advance(hours=2)
    assert (timezone.now() - start).total_seconds() == 7200
    assert isinstance(fake_clock, FakeClock)
