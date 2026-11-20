"""W2 follow-ups (no DB): verify_gemini command (injected lister, never the network) and durable log alias."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai import recorder
from apps.ai.management.commands import verify_gemini as cmd
from apps.ai.transport import TransportError


def _catalogue(*ids, actions=("generateContent", "countTokens")):
    return lambda: [{"id": i, "display_name": i, "actions": list(actions), "input_token_limit": 1_048_576,
                     "output_token_limit": 65_536} for i in ids]


@pytest.fixture
def models(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL-0000"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = "gemini-3.5-flash-lite"
    settings.GEMINI_MODEL_REASONING = "gemini-3.8-flash"
    settings.GEMINI_MODEL_VISION = "gemini-3.5-flash-lite"
    return settings


@pytest.fixture
def no_network(monkeypatch):
    def boom():
        raise AssertionError("verify_gemini must not reach the default (network) lister in tests")

    monkeypatch.setattr(cmd.Command, "lister", staticmethod(boom))
    return monkeypatch


def test_skips_without_key(settings, no_network):
    settings.GEMINI_API_KEY = ""
    settings.GEMINI_TIER = "free"
    out = StringIO()
    call_command("verify_gemini", stdout=out)
    assert "SKIPPED" in out.getvalue()


def test_require_key_fails_without_key(settings, no_network):
    settings.GEMINI_API_KEY = ""
    settings.GEMINI_TIER = "free"
    with pytest.raises(CommandError) as exc:
        call_command("verify_gemini", "--require-key")
    assert exc.value.returncode == 2


def test_all_models_present(models, monkeypatch):
    monkeypatch.setattr(cmd.Command, "lister",
                        staticmethod(_catalogue("gemini-3.5-flash-lite", "gemini-3.8-flash", "other")))
    out = StringIO()
    call_command("verify_gemini", stdout=out)
    text = out.getvalue()
    assert "All configured Gemini models are available" in text
    assert "GEMINI_MODEL_REASONING" in text and "MISSING" not in text


def test_missing_model_exits_1(models, monkeypatch):
    monkeypatch.setattr(cmd.Command, "lister", staticmethod(_catalogue("gemini-3.5-flash-lite")))
    out = StringIO()
    with pytest.raises(CommandError) as exc:
        call_command("verify_gemini", stdout=out)
    assert exc.value.returncode == 1
    assert "MISSING" in out.getvalue() and "gemini-3.8-flash" in out.getvalue()


def test_model_that_cannot_generate_exits_1(models, monkeypatch):
    monkeypatch.setattr(cmd.Command, "lister", staticmethod(
        _catalogue("gemini-3.5-flash-lite", "gemini-3.8-flash", actions=("embedContent",))))
    with pytest.raises(CommandError) as exc:
        call_command("verify_gemini", stdout=StringIO())
    assert exc.value.returncode == 1


def test_json_report(models, monkeypatch):
    import json

    monkeypatch.setattr(cmd.Command, "lister",
                        staticmethod(_catalogue("gemini-3.5-flash-lite", "gemini-3.8-flash")))
    out = StringIO()
    call_command("verify_gemini", "--json", stdout=out)
    body = out.getvalue()
    report = json.loads(body[: body.rindex("}") + 1])
    assert report["ok"] is True and {r["setting"] for r in report["models"]} == {
        "GEMINI_MODEL_FAST", "GEMINI_MODEL_REASONING", "GEMINI_MODEL_VISION"}


def test_listing_error_exits_2(models, monkeypatch):
    def fail():
        raise TransportError("auth", "API key not valid")

    monkeypatch.setattr(cmd.Command, "lister", staticmethod(fail))
    with pytest.raises(CommandError) as exc:
        call_command("verify_gemini", stdout=StringIO())
    assert exc.value.returncode == 2 and "auth" in str(exc.value)


# ------------------------------------------------------------------------------------------ durable alias
class _FakeAtomic:
    def __init__(self, from_testcase: bool):
        self._from_testcase = from_testcase


class _FakeConn:
    def __init__(self, test=None, blocks=()):
        self.settings_dict = {"TEST": test or {}}
        self.atomic_blocks = list(blocks)


class _FakeConnections:
    def __init__(self, mapping):
        self._m = mapping
        self.databases = dict.fromkeys(mapping)

    def __getitem__(self, alias):
        return self._m[alias]


def test_log_alias_is_durable_in_production(monkeypatch):
    monkeypatch.setattr(recorder, "connections", _FakeConnections({
        "default": _FakeConn(blocks=[_FakeAtomic(False)]), "durable": _FakeConn()}))
    assert recorder.log_alias() == "durable"


def test_log_alias_is_durable_for_mirror_outside_testcase_transaction(monkeypatch):
    monkeypatch.setattr(recorder, "connections", _FakeConnections({
        "default": _FakeConn(blocks=[]), "durable": _FakeConn(test={"MIRROR": "default"})}))
    assert recorder.log_alias() == "durable"


def test_log_alias_falls_back_inside_testcase_transaction(monkeypatch):
    monkeypatch.setattr(recorder, "connections", _FakeConnections({
        "default": _FakeConn(blocks=[_FakeAtomic(True), _FakeAtomic(False)]),
        "durable": _FakeConn(test={"MIRROR": "default"})}))
    assert recorder.log_alias() == "default"


def test_log_alias_without_durable_alias(monkeypatch):
    monkeypatch.setattr(recorder, "connections", _FakeConnections({"default": _FakeConn()}))
    assert recorder.log_alias() == "default"


def test_log_alias_is_always_a_configured_alias():
    from django.db import connections

    alias = recorder.log_alias()
    assert alias in connections.databases
    if "durable" in connections.databases:
        assert connections.databases["durable"].get("TEST", {}).get("MIRROR") in (None, "default")
