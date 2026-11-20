"""The chat pipeline (`apps.bi.services.ask`) without a database: every other agent's service is faked and the
DB touch points (tenant steps, audit insert, message finalisation) are recorded instead of written."""

from __future__ import annotations

import uuid
from contextlib import nullcontext

import pytest

from apps.bi import services
from apps.bi.tests.fakes import (
    DATASET_ID,
    DATASET_VERSION_ID,
    UNKNOWN_DATASET_ID,
    FakeQueryError,
    FakeTimeout,
    install,
    make_policy,
    make_semantics,
)
from nexora.contracts import ChartHint, SqlProposal

ORG = uuid.UUID("01930000-0000-7000-8000-0000000000a1")
USER = uuid.UUID("01930000-0000-7000-8000-0000000000b1")
GOOD_SQL = "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region"


class Recorder:
    def __init__(self):
        self.audits: list[dict] = []
        self.finished: list[dict] = []

    def write_audit(self, audit_id, info, **kw):
        self.audits.append({"id": audit_id, "info": info, **kw})

    def finish(self, chat, **fields):
        self.finished.append(fields)
        chat.finished = True


@pytest.fixture
def env(monkeypatch):
    fake = install(monkeypatch, governance=True, org_id=ORG)
    rec = Recorder()
    monkeypatch.setattr(services, "_step", lambda turn: nullcontext())
    monkeypatch.setattr(services, "write_audit", rec.write_audit)
    monkeypatch.setattr(services._Chat, "finish", lambda chat, **f: rec.finish(chat, **f))
    monkeypatch.setattr(services, "history_for", lambda conv_id, exclude=None: (
        {"question": "revenue by month", "sql": "SELECT 1", "columns": ["month", "revenue"]},))
    return fake, rec


def make_turn(question="revenue by region", dataset_id=DATASET_ID) -> services.Turn:
    return services.Turn(org_id=ORG, user_id=USER, conversation_id=uuid.uuid4(), dataset_id=dataset_id,
                         question=question, user_message_id=uuid.uuid4(), message_id=uuid.uuid4())


def events_of(turn=None):
    return list(services.ask(turn or make_turn()))


def names(events):
    return [e if e != "status" else f"status:{d['stage']}" for e, d in events]


# ------------------------------------------------------------------------------------------------ happy path
def test_happy_path_event_order_and_payloads(env):
    fake, rec = env
    events = events_of()
    seq = names(events)
    assert seq[:7] == ["status:authorizing", "status:resolving", "status:generating", "status:validating", "sql",
                       "status:executing", "result"]
    assert seq[7] == "status:explaining"
    assert seq[-2:] == ["token", "done"]
    assert set(seq[8:-2]) <= {"insight"} and "insight" in seq
    data = dict(events)
    assert data["sql"]["raw_sql"] == GOOD_SQL
    assert data["sql"]["executed_sql"].startswith("SELECT * FROM (")
    assert data["sql"]["source"] == "builder"
    assert data["sql"]["query_audit_id"] == str(rec.audits[-1]["id"])
    result = data["result"]
    assert result["columns"] == [{"name": "region", "type": "text", "masked": False},
                                 {"name": "revenue", "type": "numeric", "masked": False}]
    assert result["rows"][0] == ["West", 5400.5] and result["row_count"] == 4 and result["truncated"] is False
    assert result["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}
    assert data["done"]["message_id"] and data["done"]["status"] == "ok"
    # audit: one ok row, carrying the policy fingerprint and the guarded SQL, no values
    (audit,) = rec.audits
    assert audit["verdict"] == "ok" and audit["raw_sql"] == GOOD_SQL and audit["result"].row_count == 4
    assert audit["policy"].fingerprint == "f" * 64
    assert audit["info"].source == "builder" and audit["info"].purpose == "chat"
    # message finalised once with the answer
    (final,) = rec.finished
    assert final["status"] == "ok" and final["sql_raw"] == GOOD_SQL and final["query_audit_id"] == audit["id"]
    assert final["content"] == data["token"]["text"] and "West" in final["content"]
    assert final["dataset_version_id"] == DATASET_VERSION_ID
    # access history + cache
    assert ("access" in fake.names())
    assert fake.remembered and fake.remembered[0][1] == GOOD_SQL
    # the nl2sql cache is scoped to the dataset VERSION through the DatasetSemantics (AI/ML W2 request)
    assert fake.remembered[0][2]["semantics"].dataset_version_id == DATASET_VERSION_ID
    assert "dataset_version_id" not in fake.remembered[0][2]
    assert fake.propose_semantics[0] is fake.semantics[DATASET_ID]


def test_context_is_policy_filtered_and_value_free(env):
    fake, _ = env
    events_of()
    ctx = next(c for c in fake.calls if c[0] == "propose")[2]
    cols = [c["name"] for c in ctx.columns_meta["sales"]]
    assert "margin" not in cols  # denied column never reaches nl2sql
    assert {c["name"]: c["masked"] for c in ctx.columns_meta["sales"]}["customer_email"] is True
    assert [m["key"] for m in ctx.metrics] == ["revenue"]  # metric over a denied column dropped
    assert ctx.history == ({"question": "revenue by month", "sql": "SELECT 1", "columns": ["month", "revenue"]},)
    assert ctx.dataset_name == "Synthetic sales" and ctx.dataset_classification == "synthetic"
    assert next(c for c in fake.calls if c[0] == "propose")[3] == DATASET_VERSION_ID


def test_cache_hits_are_not_re_remembered(env):
    fake, _ = env
    fake.proposals.append(SqlProposal(sql=GOOD_SQL, source="cache_exact"))
    events = events_of()
    assert events[-1][0] == "done"
    assert fake.remembered == []


# ------------------------------------------------------------------------------------------------ repair
def test_repairable_rejection_gets_exactly_one_repair(env):
    fake, rec = env
    bad = "SELECT regoin FROM sales"
    fake.proposals.append(SqlProposal(sql=bad, source="llm"))
    fake.rejections[bad] = ("unknown_column", "column regoin does not exist")
    fake.repairs.append(SqlProposal(sql=GOOD_SQL, source="llm", chart_hint=ChartHint(type="pie", x="region",
                                                                                      y=("revenue",))))
    events = events_of()
    seq = names(events)
    assert seq.count("status:validating") == 2
    assert ("status", {"stage": "generating", "source": "llm", "repair": True}) in events
    assert [c for c in fake.calls if c[0] == "repair"] == [("repair", bad, "unknown_column")]
    assert [a["verdict"] for a in rec.audits] == ["rejected", "ok"]
    assert rec.audits[0]["code"] == "unknown_column" and rec.audits[0]["raw_sql"] == bad
    assert events[-1][0] == "done"
    assert dict(events)["result"]["chart"]["type"] == "pie"  # the repaired proposal's hint is used


def test_second_rejection_is_terminal_and_audited(env):
    fake, rec = env
    bad, still_bad = "SELECT regoin FROM sales", "SELECT reg FROM sales"
    fake.proposals.append(SqlProposal(sql=bad, source="llm"))
    fake.rejections[bad] = ("unknown_column", "no such column")
    fake.rejections[still_bad] = ("column_not_allowed", "column not allowed")
    fake.repairs.append(SqlProposal(sql=still_bad, source="llm"))
    events = events_of()
    assert len([c for c in fake.calls if c[0] == "repair"]) == 1
    assert [a["verdict"] for a in rec.audits] == ["rejected", "rejected"]
    event, data = events[-1]
    assert event == "error" and data["code"] == "column_not_allowed"
    assert data["query_audit_id"] == str(rec.audits[-1]["id"])
    assert "sql" not in names(events)  # nothing executed, no sql event for rejected SQL
    assert rec.finished[-1]["status"] == "error" and rec.finished[-1]["error_code"] == "column_not_allowed"
    assert not any(c[0] == "execute" for c in fake.calls)


def test_security_rejection_is_never_repaired(env):
    fake, rec = env
    evil = "SELECT pg_read_file('/etc/passwd')"
    fake.proposals.append(SqlProposal(sql=evil, source="llm"))
    fake.rejections[evil] = ("forbidden_function", "function pg_read_file is not allowed")
    events = events_of()
    assert not any(c[0] == "repair" for c in fake.calls)
    assert events[-1] == ("error", {"code": "forbidden_function", "detail": "function pg_read_file is not allowed",
                                    "message_id": events[-1][1]["message_id"],
                                    "query_audit_id": str(rec.audits[0]["id"])})
    assert [a["verdict"] for a in rec.audits] == ["rejected"]


def test_failed_repair_reports_the_original_rejection(env):
    fake, rec = env
    bad = "SELECT regoin FROM sales"
    fake.proposals.append(SqlProposal(sql=bad, source="llm"))
    fake.rejections[bad] = ("unknown_column", "no such column")
    fake.repairs.append(SqlProposal(sql=None, source="none", blocked_reason="ai_quota_exhausted"))
    events = events_of()
    assert events[-1][0] == "error" and events[-1][1]["code"] == "unknown_column"


def test_repairable_execution_error_is_repaired_once(env):
    fake, rec = env
    ambiguous = "SELECT region FROM sales s JOIN sales t USING (region)"
    fake.proposals.append(SqlProposal(sql=ambiguous, source="llm"))
    fake.exec_errors[ambiguous] = FakeQueryError()
    fake.repairs.append(SqlProposal(sql=GOOD_SQL, source="llm"))
    events = events_of()
    assert names(events).count("sql") == 2
    assert [a["verdict"] for a in rec.audits] == ["error", "ok"]
    assert rec.audits[0]["code"] == "execution_error"
    assert events[-1][0] == "done"


def test_non_repairable_execution_error(env):
    fake, rec = env
    fake.exec_errors["GROUP BY region"] = FakeTimeout()
    events = events_of()
    assert events[-1][0] == "error" and events[-1][1]["code"] == "query_timeout"
    assert [a["verdict"] for a in rec.audits] == ["error"]
    assert not any(c[0] == "repair" for c in fake.calls)


# ------------------------------------------------------------------------------------------------ no SQL
@pytest.mark.parametrize("reason", ["ai_policy_blocked", "ai_quota_exhausted", "ai_not_configured"])
def test_policy_block_is_an_error_event_with_a_friendly_detail(env, reason):
    fake, rec = env
    fake.proposals.append(SqlProposal(sql=None, source="none", blocked_reason=reason))
    events = events_of()
    assert names(events) == ["status:authorizing", "status:resolving", "status:generating", "error"]
    data = events[-1][1]
    assert data["code"] == reason and len(data["detail"]) > 20
    assert rec.finished[-1]["status"] == "blocked" and rec.finished[-1]["error_code"] == reason
    assert rec.audits[-1]["verdict"] == "blocked" and rec.audits[-1]["info"].source == "none"
    assert not any(c[0] in ("validate", "execute") for c in fake.calls)
    # CONTRACT "AI quota UX": only the quota error carries when the quota resets
    if reason == "ai_quota_exhausted":
        assert data["resets_at"] == fake.quota_resets_at
    else:
        assert "resets_at" not in data


def test_quota_error_without_a_known_reset_time_still_reports_the_error(env, monkeypatch):
    from apps.bi import ports

    fake, _rec = env
    monkeypatch.setattr(ports, "ai_quota_resets_at", lambda user_id=None: None)
    fake.proposals.append(SqlProposal(sql=None, source="none", blocked_reason="ai_quota_exhausted"))
    data = events_of()[-1][1]
    assert data["code"] == "ai_quota_exhausted" and data["resets_at"] is None


def test_quota_reset_time_comes_from_the_ai_status(monkeypatch):
    from apps.ai import services as ai_services
    from apps.bi import ports

    monkeypatch.setattr("django.db.transaction.atomic", lambda *a, **k: nullcontext())
    monkeypatch.setattr(ai_services, "ai_status", lambda user_id=None: {
        "quota": {"state": "exhausted", "resets_at": "2026-11-21T08:00:00+00:00"}, "resets_at": "x"})
    assert ports.ai_quota_resets_at(USER) == "2026-11-21T08:00:00+00:00"
    monkeypatch.setattr(ai_services, "ai_status", lambda user_id=None: {"resets_at": "2026-11-22T08:00:00+00:00"})
    assert ports.ai_quota_resets_at(USER) == "2026-11-22T08:00:00+00:00"

    def broken(user_id=None):
        raise RuntimeError("limiter down")

    monkeypatch.setattr(ai_services, "ai_status", broken)
    assert ports.ai_quota_resets_at(USER) is None


def test_clarification_is_returned_without_executing(env):
    fake, rec = env
    fake.proposals.append(SqlProposal(sql=None, source="llm", clarification="Which year do you mean?"))
    events = events_of()
    assert events[-2] == ("token", {"text": "Which year do you mean?"})
    assert events[-1][0] == "done" and events[-1][1]["status"] == "clarification"
    assert rec.finished[-1]["status"] == "clarification"
    assert rec.audits == [] and not any(c[0] == "validate" for c in fake.calls)


# ------------------------------------------------------------------------------------------------ authorization
def test_invisible_dataset_stops_before_resolving(env):
    fake, rec = env
    fake.acl[DATASET_ID] = False
    events = events_of()
    assert names(events) == ["status:authorizing", "error"]
    assert events[-1][1]["code"] == "not_found"
    assert not any(c[0] in ("semantics", "propose") for c in fake.calls)


def test_unknown_dataset_is_not_found(env):
    events = events_of(make_turn(dataset_id=UNKNOWN_DATASET_ID))
    assert events[-1][0] == "error" and events[-1][1]["code"] == "not_found"


def test_unexpected_exception_becomes_internal_error(env, monkeypatch):
    fake, rec = env

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(services.ports, "propose", boom)
    events = events_of()
    assert events[-1][0] == "error" and events[-1][1]["code"] == "internal_error"
    assert "secret" not in str(events)
    assert rec.finished[-1]["error_code"] == "internal_error"


def test_consumer_disconnect_finalises_message_as_cancelled(env):
    _, rec = env
    gen = services.ask(make_turn())
    assert next(gen)[1]["stage"] == "authorizing"
    gen.close()
    assert rec.finished[-1]["status"] == "error" and rec.finished[-1]["error_code"] == "cancelled"


def test_build_context_drops_denied_tables_and_columns():
    sem = make_semantics()
    policy = make_policy(deny=("margin", "customer_email"), mask=())
    ctx = services.build_context(org_id=ORG, user_id=USER, scope=services.DatasetScope(sem, policy))
    assert [c["name"] for c in ctx.columns_meta["sales"]] == ["region", "amount", "order_date"]
    assert all(c["masked"] is False for c in ctx.columns_meta["sales"])
    empty = make_policy(deny=())
    empty = type(empty)(**{**empty.__dict__, "allowed_columns": {}})
    ctx2 = services.build_context(org_id=ORG, user_id=USER, scope=services.DatasetScope(sem, empty))
    assert ctx2.columns_meta == {} and ctx2.metrics == ()
