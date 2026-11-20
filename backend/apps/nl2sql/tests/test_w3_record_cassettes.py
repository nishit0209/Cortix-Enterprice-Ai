"""`manage.py record_cassettes`: refuses without GEMINI_API_KEY, records only builder-declined golden questions in
QA's nexora.cassette.v1 format, and the cassettes replay through QA's runner to the recorded SQL.

Gemini is NEVER called: the command's engine is `standalone_engine` with a FakeTransport.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai.services import standalone_engine
from apps.ai.testing import FakeTransport
from apps.core.exceptions import AIQuotaExhausted
from apps.nl2sql import services as nl2sql_services
from apps.nl2sql.management.commands import record_cassettes as rc
from testing import golden

G46_SQL = ("SELECT SUM(revenue) AS total_revenue FROM sales WHERE category = 'Electronics' "
           "AND order_date >= DATE '2025-01-01' AND order_date < DATE '2026-01-01'")


def gen(sql, **extra):
    return FakeTransport.json({"sql": sql, "explanation": "Total revenue of Electronics in 2025.",
                               "assumptions": ["Revenue is net of discounts"],
                               "chart_hint": {"type": "kpi", "x": None, "y": ["total_revenue"], "series": None},
                               "clarification": None, **extra})


@pytest.fixture
def key(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = "gemini-3.5-flash-lite"
    settings.GEMINI_MODEL_REASONING = "gemini-3.8-flash"


@pytest.fixture
def engine_with(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(rc.Command, "sleep", staticmethod(sleeps.append))

    def install(*responses):
        transport = FakeTransport(list(responses))
        monkeypatch.setattr(rc.Command, "make_engine", staticmethod(
            lambda: standalone_engine(transport_factory=lambda: transport, sleep=lambda s: None)))
        return transport

    install.sleeps = sleeps
    return install


def read_v1(path, qid):
    """QA's loader rules (testing/golden.py W3): format, id, question, prompt_key, response dict."""
    data = json.loads(path.read_text(encoding="utf-8"))
    case = next(c for c in golden.golden()["questions"] if c["id"] == qid)
    assert data["format"] == "nexora.cassette.v1"
    assert data["id"] == qid and data["question"] == case["question"]
    assert data["prompt_key"] == "nl2sql.generate" and isinstance(data["response"], dict)
    return data, case


def replay_is_correct(data, case) -> str:
    sql = golden.cassette_sql(case["question"], data)
    columns, rows = golden.run_sql(sql)
    assert golden.compare(golden.expected_result(case["check"]), columns, rows) == []
    return sql


def test_refuses_without_a_key(settings, tmp_path, monkeypatch):
    settings.GEMINI_API_KEY = ""
    monkeypatch.setattr(rc.Command, "make_engine", staticmethod(lambda: pytest.fail("must not build an engine")))
    with pytest.raises(CommandError) as exc:
        call_command("record_cassettes", out=str(tmp_path))
    assert exc.value.returncode == 2 and "GEMINI_API_KEY" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_records_builder_declined_questions_only(key, tmp_path, engine_with, capsys):
    transport = engine_with(gen(G46_SQL))
    call_command("record_cassettes", out=str(tmp_path), only="g01,g46")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["g46.json"]  # g01 is answered by the builder
    data, case = read_v1(tmp_path / "g46.json", "g46")
    assert data["repaired"] is False and data["model"] == "gemini-3.5-flash-lite" and data["recorded_at"]
    assert data["response"]["sql"] == G46_SQL
    assert transport.total_calls == 1
    request = transport.calls[0]
    assert case["question"] in request.prompt and "2026-11-17" in request.prompt  # golden anchor date
    sql = replay_is_correct(data, case)
    assert "Electronics" in sql
    out = capsys.readouterr().out
    assert "g01: answered by the builder" in out and "recorded 1, answered by the builder 1" in out


def test_a_repaired_answer_is_stored_as_the_generate_response(key, tmp_path, engine_with):
    bad = G46_SQL.replace("SUM(revenue)", "SUM(revenu)")  # unknown column -> one repair call
    transport = engine_with(gen(bad), gen(G46_SQL))
    call_command("record_cassettes", out=str(tmp_path), only="g46")
    data, case = read_v1(tmp_path / "g46.json", "g46")
    assert data["repaired"] is True and data["response"]["sql"] == G46_SQL
    assert transport.total_calls == 2
    replay_is_correct(data, case)  # replays with ONE nl2sql.generate response


def test_existing_cassettes_are_kept_unless_forced(key, tmp_path, engine_with):
    target = tmp_path / "nl2sql" / "g46.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    transport = engine_with(gen(G46_SQL))
    call_command("record_cassettes", out=str(tmp_path), only="g46", layout="nl2sql")
    assert target.read_text(encoding="utf-8") == "{}" and transport.total_calls == 0
    call_command("record_cassettes", out=str(tmp_path), only="g46", layout="nl2sql", force=True)
    read_v1(target, "g46")


def test_rate_limits_are_waited_out(key, tmp_path, engine_with, monkeypatch):
    engine_with(gen(G46_SQL))
    real = nl2sql_services._generate
    calls = []

    def flaky(question, ctx, today):
        calls.append(question)
        if len(calls) == 1:
            exc = AIQuotaExhausted("busy", code="ai_rate_limited",
                                   extra={"scope": "project_minute", "resets_at": "2026-11-17T10:01:00+00:00",
                                          "retry_after_s": 7})
            return nl2sql_services._none("ai_rate_limited", exc=exc)
        return real(question, ctx, today)

    monkeypatch.setattr(nl2sql_services, "_generate", flaky)
    call_command("record_cassettes", out=str(tmp_path), only="g46")
    assert engine_with.sleeps == [8.0] and len(calls) == 2
    read_v1(tmp_path / "g46.json", "g46")


def test_quota_exhaustion_stops_and_fails(key, tmp_path, engine_with, monkeypatch):
    engine_with()
    seen = []

    def exhausted(question, ctx, today):
        seen.append(question)
        return nl2sql_services._none("ai_quota_exhausted")

    monkeypatch.setattr(nl2sql_services, "_generate", exhausted)
    with pytest.raises(CommandError) as exc:
        call_command("record_cassettes", out=str(tmp_path), only="g46,g47,g48")
    assert exc.value.returncode == 1 and "g46" in str(exc.value)
    assert len(seen) == 1 and list(tmp_path.iterdir()) == []


def test_unknown_ids_are_rejected(key, tmp_path, engine_with):
    engine_with()
    with pytest.raises(CommandError) as exc:
        call_command("record_cassettes", out=str(tmp_path), only="g999")
    assert exc.value.returncode == 2
