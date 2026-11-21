"""Conversational BI API end to end (runs as nexora_app: RLS enforced). Governance is REAL; the semantic layer,
SQL guard / executor and NL->SQL are in-memory fakes (apps/bi/tests/fakes.py). No Gemini, no warehouse."""

from __future__ import annotations

import io
import json

import pytest
from django.db import DatabaseError, connection, transaction
from rest_framework.test import APIClient

from apps.accounts.services import create_api_key
from apps.bi.models import BiQueryAudit, Conversation, Message, SavedQuery
from apps.bi.tests.fakes import DANGEROUS_RESULT, DATASET_ID, UNKNOWN_DATASET_ID, install
from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url
from apps.governance.models import AccessHistory, DatasetAcl
from apps.orgs.models import Role
from nexora.contracts import SqlProposal
from testing.security import unescaped_formula_cells

pytestmark = pytest.mark.django_db

GOOD_SQL = "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region"


@pytest.fixture
def fake(monkeypatch):
    return install(monkeypatch)


@pytest.fixture
def analyst(org):
    return member_of(org, "analyst")


@pytest.fixture
def analyst_client(analyst):
    return client_for(analyst)


def new_conversation(client, org, dataset_id=DATASET_ID, **extra) -> dict:
    r = client.post(org_url(org, "bi/conversations"), {"dataset_id": str(dataset_id), **extra}, format="json")
    assert r.status_code == 201, r.content
    return r.json()


def ask(client, org, conv_id, question="revenue by region", **headers):
    return client.post(org_url(org, f"bi/conversations/{conv_id}/messages"), {"question": question}, format="json",
                       **headers)


def sse_events(resp) -> list[tuple[str, dict]]:
    text = b"".join(resp.streaming_content).decode()
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in frame.split("\n"))
        out.append((fields["event"], json.loads(fields["data"])))
    return out


# ================================================================================================ conversations
def test_conversation_crud_is_owner_only(org, owner, owner_client, analyst_client, fake):
    conv = new_conversation(owner_client, org, title="Q3 revenue")
    assert set(conv) == {"id", "title", "dataset_id", "created_at", "last_message_at"}
    assert conv["title"] == "Q3 revenue" and conv["dataset_id"] == str(DATASET_ID)
    listed = owner_client.get(org_url(org, "bi/conversations")).json()
    assert [c["id"] for c in listed["items"]] == [conv["id"]]
    assert analyst_client.get(org_url(org, "bi/conversations")).json()["items"] == []
    detail = org_url(org, f"bi/conversations/{conv['id']}")
    for method in ("get", "patch", "delete"):
        assert getattr(analyst_client, method)(detail, {"title": "x"}, format="json").status_code == 404
    assert analyst_client.get(detail + "/messages").status_code == 404
    assert ask(analyst_client, org, conv["id"]).status_code == 404
    r = owner_client.patch(detail, {"title": "Renamed"}, format="json")
    assert r.status_code == 200 and r.json()["title"] == "Renamed"
    assert owner_client.delete(detail).status_code == 204
    assert owner_client.get(detail).status_code == 404
    with tenant_context(org.id):
        actions = set(AuditEvent.objects.filter(action__startswith="bi.conversation").values_list("action", flat=True))
    assert actions == {"bi.conversation.create", "bi.conversation.update", "bi.conversation.delete"}


def test_conversation_needs_a_visible_dataset(org, owner, analyst, analyst_client, fake):
    r = analyst_client.post(org_url(org, "bi/conversations"), {"dataset_id": str(UNKNOWN_DATASET_ID)}, format="json")
    assert r.status_code == 404
    with tenant_context(org.id):  # an ACL that grants only viewers -> analysts can no longer see the dataset
        DatasetAcl.objects.create(dataset_id=DATASET_ID, principal_type="role",
                                  principal_id=Role.objects.get(key="viewer").id, privilege="query")
    r = analyst_client.post(org_url(org, "bi/conversations"), {"dataset_id": str(DATASET_ID)}, format="json")
    assert r.status_code == 404
    assert analyst_client.post(org_url(org, "bi/conversations"), {"dataset_id": "nope"},
                               format="json").status_code == 400


def test_permissions_and_cross_org(org, other_org, owner_client, fake):
    auditor = client_for(member_of(org, "auditor"))
    assert auditor.get(org_url(org, "bi/conversations")).status_code == 403  # no bi.chat
    assert APIClient().get(org_url(org, "bi/conversations")).status_code == 401
    conv = new_conversation(owner_client, org)
    with tenant_context(other_org.id):
        from apps.orgs.models import Membership

        other_owner = Membership.objects.get().user
    other = client_for(other_owner)
    assert other.get(org_url(org, "bi/conversations")).status_code == 404  # not a member
    assert other.get(org_url(other_org, f"bi/conversations/{conv['id']}")).status_code == 404  # IDOR via own slug
    assert ask(other, other_org, conv["id"]).status_code == 404


# ================================================================================================ chat
def test_ask_json_returns_the_answered_message(org, owner, owner_client, fake):
    conv = new_conversation(owner_client, org)
    r = ask(owner_client, org, conv["id"])
    assert r.status_code == 200, r.content
    msg = r.json()
    assert msg["role"] == "assistant" and msg["status"] == "ok" and msg["error_code"] is None
    assert msg["sql"]["raw"] == GOOD_SQL and msg["sql"]["source"] == "builder"
    assert msg["sql"]["executed"].startswith("SELECT * FROM (")
    assert msg["result"]["row_count"] == 4 and msg["result"]["rows"][0] == ["West", 5400.5]
    assert msg["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}
    assert msg["insights"] and all({"kind", "text"} <= set(i) for i in msg["insights"])
    assert msg["assumptions"] == ["All time."]
    assert "West" in msg["content"]
    listed = owner_client.get(org_url(org, f"bi/conversations/{conv['id']}/messages")).json()["items"]
    assert [m["role"] for m in listed] == ["user", "assistant"]
    assert listed[0]["content"] == "revenue by region"
    with tenant_context(org.id):
        audit = BiQueryAudit.objects.get()
        assert str(audit.id) == msg["query_audit_id"]
        assert audit.verdict == "ok" and audit.source == "builder" and audit.purpose == "chat"
        assert audit.user_id == owner.pk and audit.dataset_id == DATASET_ID and audit.row_count == 4
        assert audit.raw_sql == GOOD_SQL and audit.executed_sql == msg["sql"]["executed"]
        assert len(audit.policy_fingerprint) == 64 and audit.executing_role == "wh_q_fake"
        assert "West" not in json.dumps([audit.params, audit.message, audit.question])  # no values audited
        access = AccessHistory.objects.get()
        assert access.access_type == "query" and access.query_audit_id == audit.id
        assert Conversation.objects.get().title == "revenue by region"
    assert fake.remembered and fake.remembered[0][1] == GOOD_SQL


def test_ask_streams_server_sent_events(org, owner_client, fake):
    conv = new_conversation(owner_client, org)
    resp = ask(owner_client, org, conv["id"], HTTP_ACCEPT="text/event-stream")
    assert resp.status_code == 200
    assert resp.streaming
    assert resp["Content-Type"].startswith("text/event-stream")
    assert resp["X-Accel-Buffering"] == "no"
    assert "no-cache" in resp["Cache-Control"]
    events = sse_events(resp)
    kinds = [e if e != "status" else f"status:{d['stage']}" for e, d in events]
    assert kinds[:7] == ["status:authorizing", "status:resolving", "status:generating", "status:validating", "sql",
                         "status:executing", "result"]
    assert kinds[-2:] == ["token", "done"]
    done = events[-1][1]
    sql = dict(events)["sql"]
    with tenant_context(org.id):
        msg = Message.objects.get(id=done["message_id"])
        assert msg.status == "ok" and str(msg.query_audit_id) == sql["query_audit_id"]
        assert BiQueryAudit.objects.filter(id=sql["query_audit_id"], verdict="ok").exists()


def test_sse_errors_before_streaming_are_json_problems(org, owner_client, analyst_client, fake):
    conv = new_conversation(owner_client, org)
    r = ask(analyst_client, org, conv["id"], HTTP_ACCEPT="text/event-stream")
    assert r.status_code == 404 and not r.streaming
    assert r["Content-Type"].startswith("application/problem+json")
    r = owner_client.post(org_url(org, f"bi/conversations/{conv['id']}/messages"), {"question": ""}, format="json",
                          HTTP_ACCEPT="text/event-stream")
    assert r.status_code == 400 and not r.streaming


def test_guard_rejection_is_audited_and_reported(org, owner_client, fake):
    evil = "SELECT pg_read_file('/etc/passwd')"
    fake.proposals.append(SqlProposal(sql=evil, source="llm"))
    fake.rejections[evil] = ("forbidden_function", "function pg_read_file is not allowed")
    conv = new_conversation(owner_client, org)
    msg = ask(owner_client, org, conv["id"]).json()
    assert msg["status"] == "error" and msg["error_code"] == "forbidden_function"
    assert msg["result"] is None
    with tenant_context(org.id):
        audit = BiQueryAudit.objects.get()
        assert audit.verdict == "rejected" and audit.reject_code == "forbidden_function" and audit.raw_sql == evil
        assert not AccessHistory.objects.exists()
    assert not any(c[0] == "execute" for c in fake.calls)
    assert not any(c[0] == "repair" for c in fake.calls)


def test_repair_once_through_the_api(org, owner_client, fake):
    bad = "SELECT regoin FROM sales"
    fake.proposals.append(SqlProposal(sql=bad, source="llm"))
    fake.rejections[bad] = ("unknown_column", "column regoin does not exist")
    fake.repairs.append(SqlProposal(sql=GOOD_SQL, source="llm"))
    conv = new_conversation(owner_client, org)
    events = sse_events(ask(owner_client, org, conv["id"], HTTP_ACCEPT="text/event-stream"))
    assert events[-1][0] == "done"
    with tenant_context(org.id):
        assert list(BiQueryAudit.objects.order_by("occurred_at", "id").values_list("verdict", flat=True)) == [
            "rejected", "ok"]
    assert len([c for c in fake.calls if c[0] == "repair"]) == 1


def test_policy_block_is_reported_with_its_code(org, owner_client, fake):
    fake.proposals.append(SqlProposal(sql=None, source="none", blocked_reason="ai_policy_blocked",
                                      explanation="Your organization's AI data policy does not allow this."))
    conv = new_conversation(owner_client, org)
    events = sse_events(ask(owner_client, org, conv["id"], HTTP_ACCEPT="text/event-stream"))
    assert events[-1] == ("error", {"code": "ai_policy_blocked",
                                    "detail": "Your organization's AI data policy does not allow this.",
                                    "message_id": events[-1][1]["message_id"],
                                    "query_audit_id": events[-1][1]["query_audit_id"]})
    with tenant_context(org.id):
        msg = Message.objects.get(id=events[-1][1]["message_id"])
        assert msg.status == "blocked" and msg.error_code == "ai_policy_blocked"
        assert BiQueryAudit.objects.get().verdict == "blocked"


def test_follow_up_history_carries_no_values(org, owner_client, fake):
    conv = new_conversation(owner_client, org)
    ask(owner_client, org, conv["id"], "revenue by region")
    ask(owner_client, org, conv["id"], "same but by month")
    ctx = [c for c in fake.calls if c[0] == "propose"][-1][2]
    assert ctx.history == ({"question": "revenue by region", "sql": GOOD_SQL, "columns": ["region", "revenue"]},)
    assert "West" not in json.dumps(ctx.history)


def test_revoked_dataset_access_stops_the_chat(org, owner_client, analyst, analyst_client, fake):
    conv = new_conversation(analyst_client, org)
    with tenant_context(org.id):
        DatasetAcl.objects.create(dataset_id=DATASET_ID, principal_type="role",
                                  principal_id=Role.objects.get(key="viewer").id, privilege="query")
    assert ask(analyst_client, org, conv["id"]).status_code == 404


def test_api_key_chat_is_attributed_to_the_key(org, owner, fake):
    with tenant_context(org.id):
        key, token = create_api_key(name="synthetic-bot", scopes=["bi.chat"], created_by=owner)
        conv = Conversation.objects.create(user_id=owner.pk, dataset_id=DATASET_ID)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    r = ask(client, org, conv.id)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    with tenant_context(org.id):
        assert BiQueryAudit.objects.get().api_key_id == key.id


# ================================================================================================ manual SQL
def test_manual_query_runs_through_the_guard(org, analyst, analyst_client, fake):
    r = analyst_client.post(org_url(org, "bi/queries"), {"dataset_id": str(DATASET_ID), "sql": GOOD_SQL}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["row_count"] == 4 and body["columns"][0]["name"] == "region" and body["truncated"] is False
    assert body["executed_sql"].startswith("SELECT * FROM (")
    with tenant_context(org.id):
        audit = BiQueryAudit.objects.get(id=body["query_audit_id"])
        assert audit.source == "manual" and audit.purpose == "manual" and audit.user_id == analyst.pk


def test_manual_query_rejection_is_422_and_stays_audited(org, analyst_client, fake):
    evil = "SELECT * FROM pg_shadow"
    fake.rejections[evil] = ("system_object", "system catalogs are not allowed")
    r = analyst_client.post(org_url(org, "bi/queries"), {"dataset_id": str(DATASET_ID), "sql": evil}, format="json")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "system_object" and body["query_audit_id"]
    with tenant_context(org.id):  # survived the error response (rollback_on_error = False)
        audit = BiQueryAudit.objects.get(id=body["query_audit_id"])
        assert audit.verdict == "rejected" and audit.reject_code == "system_object"


def test_manual_query_permissions(org, other_org, fake):
    viewer = client_for(member_of(org, "viewer"))  # bi.chat but not bi.sql
    payload = {"dataset_id": str(DATASET_ID), "sql": GOOD_SQL}
    assert viewer.post(org_url(org, "bi/queries"), payload, format="json").status_code == 403
    outsider = client_for(member_of(other_org, "analyst"))
    assert outsider.post(org_url(org, "bi/queries"), payload, format="json").status_code == 404
    analyst = client_for(member_of(org, "analyst"))
    bad = {"dataset_id": str(UNKNOWN_DATASET_ID), "sql": GOOD_SQL}
    assert analyst.post(org_url(org, "bi/queries"), bad, format="json").status_code == 404


def test_query_history_own_vs_audit_view(org, owner, owner_client, analyst, analyst_client, fake):
    analyst_client.post(org_url(org, "bi/queries"), {"dataset_id": str(DATASET_ID), "sql": GOOD_SQL}, format="json")
    conv = new_conversation(owner_client, org)
    ask(owner_client, org, conv["id"])
    own = analyst_client.get(org_url(org, "bi/query-history")).json()["items"]
    assert [i["user_email"] for i in own] == [analyst.email]
    item = own[0]
    assert item["dataset"] == {"id": str(DATASET_ID), "name": "Synthetic sales"}
    assert {"occurred_at", "question", "source", "verdict", "reject_code", "executed_sql", "row_count",
            "duration_ms"} <= set(item)
    auditor = client_for(member_of(org, "auditor"))
    everything = auditor.get(org_url(org, "bi/query-history")).json()["items"]
    assert {i["user_email"] for i in everything} == {analyst.email, owner.email}
    only_owner = auditor.get(org_url(org, f"bi/query-history?user_id={owner.pk}")).json()["items"]
    assert {i["user_email"] for i in only_owner} == {owner.email}
    assert auditor.get(org_url(org, "bi/query-history?user_id=nope")).status_code == 400


# ================================================================================================ feedback
def test_thumbs_up_promotes_a_verified_answer(org, owner_client, analyst_client, fake):
    conv = new_conversation(owner_client, org)
    msg = ask(owner_client, org, conv["id"]).json()
    url = org_url(org, f"bi/messages/{msg['id']}/feedback")
    assert analyst_client.post(url, {"value": "up"}, format="json").status_code == 404
    r = owner_client.post(url, {"value": "up"}, format="json")
    assert r.status_code == 200 and r.json() == {"message_id": msg["id"], "feedback": "up", "verified": True}
    (question, sql, ctx, kwargs) = fake.verified[0]
    assert question == "revenue by region" and sql == GOOD_SQL
    assert ctx.dataset_id == DATASET_ID and kwargs["chart_hint"].type == "bar"
    r = owner_client.post(url, {"value": "down"}, format="json")
    assert r.json()["verified"] is False and len(fake.verified) == 1
    assert owner_client.post(url, {"value": "meh"}, format="json").status_code == 400
    with tenant_context(org.id):
        assert Message.objects.get(id=msg["id"]).feedback == "down"
        assert AuditEvent.objects.filter(action="bi.message.feedback").count() == 2


def test_thumbs_up_outside_current_policy_is_stored_but_not_verified(org, owner_client, fake):
    conv = new_conversation(owner_client, org)
    msg = ask(owner_client, org, conv["id"]).json()
    fake.verify_error = ValueError("outside policy")
    r = owner_client.post(org_url(org, f"bi/messages/{msg['id']}/feedback"), {"value": "up"}, format="json")
    assert r.status_code == 200 and r.json()["verified"] is False
    assert r.json()["not_verified_reason"] == "outside_policy"


# ================================================================================================ exports
def _answered(client, org, fake) -> dict:
    conv = new_conversation(client, org)
    return ask(client, org, conv["id"]).json()


def test_csv_export_reexecutes_via_the_guard_and_escapes_formulas(org, owner, owner_client, fake):
    msg = _answered(owner_client, org, fake)
    fake.result = DANGEROUS_RESULT  # the re-execution returns hostile cells: they must be neutralised
    validations_before = len([c for c in fake.calls if c[0] == "validate"])
    r = owner_client.post(org_url(org, "exports"), {"message_id": msg["id"], "format": "csv"}, format="json")
    assert r.status_code == 200, r.content
    assert r["Content-Type"].startswith("text/csv")
    disposition = r["Content-Disposition"]
    assert disposition.startswith('attachment; filename="revenue-by-region-') and "filename*=UTF-8''" in disposition
    text = b"".join(r.streaming_content).decode("utf-8").lstrip("﻿")
    assert unescaped_formula_cells(text) == []
    assert "'=HYPERLINK" in text
    validations = [c for c in fake.calls if c[0] == "validate"][validations_before:]
    assert validations == [("validate", GOOD_SQL)]  # the AUDITED raw SQL is re-validated, stored rows never reused
    with tenant_context(org.id):
        export_audit = BiQueryAudit.objects.get(purpose="export")
        assert export_audit.verdict == "ok" and r["X-Query-Audit-Id"] == str(export_audit.id)
        assert AccessHistory.objects.filter(access_type="export").count() == 1
        ev = AuditEvent.objects.get(action="bi.export")
        assert ev.metadata["format"] == "csv" and ev.metadata["row_count"] == 6


def test_xlsx_export(org, owner_client, fake):
    from openpyxl import load_workbook

    msg = _answered(owner_client, org, fake)
    fake.result = DANGEROUS_RESULT
    r = owner_client.post(org_url(org, "exports"), {"message_id": msg["id"], "format": "xlsx"}, format="json")
    assert r.status_code == 200
    assert r["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ws = load_workbook(io.BytesIO(r.content)).active
    labels = [row[0].value for row in ws.iter_rows(min_row=2)]
    assert labels[0].startswith("'=") and all(row[0].data_type == "s" for row in ws.iter_rows(min_row=2))


def test_export_permissions_ownership_and_state(org, owner_client, analyst, analyst_client, fake):
    msg = _answered(owner_client, org, fake)
    url = org_url(org, "exports")
    viewer = client_for(member_of(org, "viewer"))  # no exports.create
    assert viewer.post(url, {"message_id": msg["id"], "format": "csv"}, format="json").status_code == 403
    assert analyst_client.post(url, {"message_id": msg["id"], "format": "csv"}, format="json").status_code == 404
    assert owner_client.post(url, {"message_id": msg["id"], "format": "pdf"}, format="json").status_code == 400
    fake.proposals.append(SqlProposal(sql=None, source="none", blocked_reason="ai_quota_exhausted"))
    conv = new_conversation(owner_client, org)
    failed = ask(owner_client, org, conv["id"]).json()
    r = owner_client.post(url, {"message_id": failed["id"], "format": "csv"}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "export_not_available"


def test_export_uses_the_current_export_privilege(org, analyst, analyst_client, fake):
    msg = _answered(analyst_client, org, fake)
    with tenant_context(org.id):  # analysts may now only QUERY this dataset, not export it
        DatasetAcl.objects.create(dataset_id=DATASET_ID, principal_type="role",
                                  principal_id=Role.objects.get(key="analyst").id, privilege="query")
    r = analyst_client.post(org_url(org, "exports"), {"message_id": msg["id"], "format": "csv"}, format="json")
    assert r.status_code == 404


def test_export_rejected_on_reexecution_is_audited(org, owner_client, fake):
    msg = _answered(owner_client, org, fake)
    fake.rejections[GOOD_SQL] = ("column_not_allowed", "column revenue is not allowed")
    r = owner_client.post(org_url(org, "exports"), {"message_id": msg["id"], "format": "csv"}, format="json")
    assert r.status_code == 422 and r.json()["code"] == "column_not_allowed"
    with tenant_context(org.id):
        assert BiQueryAudit.objects.get(purpose="export").verdict == "rejected"


# ================================================================================================ insights
def test_dashboard_insights_are_the_callers_own(org, owner_client, analyst_client, fake):
    _answered(owner_client, org, fake)
    items = owner_client.get(org_url(org, "insights")).json()["items"]
    assert items and {"kind", "title", "text", "dataset_id"} <= set(items[0])
    assert items[0]["dataset_id"] == str(DATASET_ID)
    assert analyst_client.get(org_url(org, "insights")).json()["items"] == []


# ================================================================================================ saved queries
def test_saved_queries_crud_and_sharing(org, owner, owner_client, analyst_client, fake):
    url = org_url(org, "saved-queries")
    payload = {"name": "Revenue by region", "dataset_id": str(DATASET_ID), "sql": GOOD_SQL,
               "chart": {"type": "bar", "x": "region", "y": ["revenue"]}}
    r = owner_client.post(url, payload, format="json")
    assert r.status_code == 201, r.content
    sq = r.json()
    assert sq["owner_id"] == str(owner.pk) and sq["is_shared"] is False
    assert owner_client.post(url, payload, format="json").status_code == 400  # duplicate name
    detail = f"{url}/{sq['id']}"
    assert analyst_client.get(detail).status_code == 404  # private
    assert owner_client.patch(detail, {"is_shared": True}, format="json").json()["is_shared"] is True
    assert analyst_client.get(detail).status_code == 200
    assert [i["id"] for i in analyst_client.get(url).json()["items"]] == [sq["id"]]
    assert analyst_client.patch(detail, {"name": "mine"}, format="json").status_code == 403
    assert analyst_client.delete(detail).status_code == 403
    run = analyst_client.post(f"{detail}/run")
    assert run.status_code == 200 and run.json()["row_count"] == 4
    assert run.json()["chart"]["type"] == "bar"
    with tenant_context(org.id):
        assert BiQueryAudit.objects.filter(purpose="saved_query", verdict="ok").count() == 1
    assert owner_client.delete(detail).status_code == 204
    with tenant_context(org.id):
        assert not SavedQuery.objects.exists()


def test_saved_query_sql_is_guarded_on_save(org, owner_client, fake):
    evil = "DELETE FROM sales"
    fake.rejections[evil] = ("not_select", "only SELECT is allowed")
    r = owner_client.post(org_url(org, "saved-queries"),
                          {"name": "evil", "dataset_id": str(DATASET_ID), "sql": evil}, format="json")
    assert r.status_code == 422 and r.json()["code"] == "not_select"
    with tenant_context(org.id):
        assert not SavedQuery.objects.exists()
        assert BiQueryAudit.objects.get().verdict == "rejected"
    viewer = client_for(member_of(org, "viewer"))
    assert viewer.post(org_url(org, "saved-queries"), {"name": "x", "dataset_id": str(DATASET_ID), "sql": GOOD_SQL},
                       format="json").status_code == 403


def test_saved_query_writes_need_dashboards_manage_like_pinning(org, fake):
    """W4 rule: every saved-query write (create/patch/delete) needs `dashboards.manage`, the same permission that
    pinning an answer (which also creates a SavedQuery) needs; `dashboards.create` alone grants nothing."""
    from apps.core.tests.fixtures import member_with_permissions

    body = {"name": "q", "dataset_id": str(DATASET_ID), "sql": GOOD_SQL}
    legacy = client_for(member_with_permissions(org, {"org.view", "dashboards.view", "dashboards.create"}))
    assert legacy.post(org_url(org, "saved-queries"), body, format="json").status_code == 403
    manager = client_for(member_with_permissions(org, {"org.view", "dashboards.view", "dashboards.manage"}))
    r = manager.post(org_url(org, "saved-queries"), body, format="json")
    assert r.status_code == 201, r.content
    qid = r.json()["id"]
    assert manager.patch(org_url(org, f"saved-queries/{qid}"), {"name": "renamed"}, format="json").status_code == 200
    assert manager.delete(org_url(org, f"saved-queries/{qid}")).status_code == 204


# ================================================================================================ storage guarantees
def test_bi_rows_are_tenant_isolated(org, other_org, owner_client, fake):
    _answered(owner_client, org, fake)
    with tenant_context(other_org.id):
        assert not Conversation.objects.exists()
        assert not Message.objects.exists()
        assert not BiQueryAudit.objects.exists()
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM bi_biqueryaudit")
            assert cur.fetchone()[0] == 0


def test_query_audit_is_append_only(org, owner_client, fake):
    _answered(owner_client, org, fake)
    with tenant_context(org.id):
        audit = BiQueryAudit.objects.get()
        with pytest.raises(DatabaseError), transaction.atomic():
            BiQueryAudit.objects.filter(id=audit.id).update(verdict="rejected")
        with pytest.raises(DatabaseError), transaction.atomic():
            BiQueryAudit.objects.filter(id=audit.id).delete()


def test_access_history_endpoint(org, owner_client, fake):
    _answered(owner_client, org, fake)
    auditor = client_for(member_of(org, "auditor"))
    items = auditor.get(org_url(org, f"access-history?asset_id={DATASET_ID}")).json()["items"]
    assert len(items) == 1 and items[0]["access_type"] == "query" and items[0]["asset_type"] == "dataset"
    assert items[0]["query_audit_id"]
    analyst = client_for(member_of(org, "analyst"))
    assert analyst.get(org_url(org, "access-history")).status_code == 403
    assert auditor.get(org_url(org, "access-history?asset_id=bad")).status_code == 400
