"""End-to-end BI flow through PUBLIC APIs and services only (TESTING_STRATEGY §3; P3-AC1 + P4 acceptance).

signup -> org -> upload testdata/generated/sales.csv -> dataset ingest (Celery eager) -> profiling + quality ->
semantic model (apps.semantic.services.dataset_semantics) -> POST bi conversation -> ask "total revenue by region"
(deterministic builder path, no nl2sql Gemini call) -> SSE events carry sql, a result with the 5 regions (+ the
NULL group of the 6 empty region cells), a chart and insights -> a BiQueryAudit row exists -> a second org cannot
see the conversation, the dataset or the audit (404 / absent).

Real Postgres (nexora_app under RLS + nexora_wh); object storage is the in-memory fake (prefix enforced);
Redis is FakeRedis; Gemini is the FakeGeminiService behind the REAL AIDataPolicy gate; outbound network is
blocked. Stages run in order; a stage whose feature is not merged yet is recorded and the test ends with a
skip that names every stage verified and every stage missing (never a silent pass).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile

from testing.client import api_client_for, route_exists
from testing.factories import TEST_PASSWORD
from testing.golden import compare, expected_result
from testing.sse import events_by_name, parse_sse, read_stream

REPO = Path(__file__).resolve().parents[3]
SALES = REPO / "testdata" / "generated" / "sales.csv"
REGIONS = {"North", "South", "East", "West", "Central"}
QUESTION = "total revenue by region"
REGION_CHECK = {"type": "group", "agg": "sum", "measure": "revenue", "by": "region"}

pytestmark = [pytest.mark.django_db, pytest.mark.ac("P3-AC1"), pytest.mark.ac("P4-AC2")]


class Flow:
    """Tracks verified and missing stages so a partial run ends with a precise skip."""

    def __init__(self):
        self.done: list[str] = []
        self.missing: list[str] = []

    def ok(self, stage: str) -> None:
        self.done.append(stage)

    def absent(self, stage: str, why: str) -> None:
        self.missing.append(f"{stage} ({why})")

    def finish(self) -> None:
        if self.missing:
            pytest.skip(f"verified: {', '.join(self.done)} | not merged yet: {'; '.join(self.missing)}")


def _signup(email: str, org_name: str):
    client = api_client_for(None)
    resp = client.post(
        "/api/v1/auth/signup",
        {"email": email, "password": TEST_PASSWORD, "full_name": "E2E Synthetic", "org_name": org_name},
        format="json",
    )
    assert resp.status_code == 201, resp.content[:500]
    body = resp.json()
    org = body["org"]
    assert org["ai_data_policy"] == "synthetic_only"
    return client, org


def _column_names(columns) -> list[str]:
    return [c["name"] if isinstance(c, dict) else str(c) for c in columns]


def _audit_model():
    for model in django_apps.get_app_config("bi").get_models():
        if model._meta.db_table == "bi_query_audit" or model.__name__.endswith("QueryAudit"):
            return model
    return None


def _semantics(org_id, dataset_id):
    try:
        from apps.semantic import services as semantic
    except ImportError:
        return None
    fn = getattr(semantic, "dataset_semantics", None)
    if fn is None:
        return None
    from apps.core.tenancy import tenant_context
    from testing.db import clear_db_tenant

    try:
        with tenant_context(org_id):
            return fn(dataset_id)
    finally:
        clear_db_tenant()


def test_signup_to_bi_answer_end_to_end(
    memory_storage, fake_redis, gated_fake_gemini, network_spy, django_capture_on_commit_callbacks
):
    flow = Flow()
    gated_fake_gemini.respond("*", stream=["Revenue is concentrated in the North and West regions."])

    # ---------------------------------------------------------------- 1. signup -> org (session + CSRF like the SPA)
    client, org = _signup("e2e-owner@example.test", "E2E Analytics")
    slug, org_id = org["slug"], org["id"]
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert [o["role"] for o in me.json()["orgs"] if o["slug"] == slug] == ["owner"]
    flow.ok("signup+org")
    base = f"/api/v1/orgs/{slug}"

    # ---------------------------------------------------------------- 2. upload sales.csv
    data = SALES.read_bytes()
    up = client.post(f"{base}/uploads", {"file": SimpleUploadedFile("sales.csv", data, "text/csv")},
                     format="multipart")
    assert up.status_code == 201, up.content[:500]
    assert up.json()["size_bytes"] == len(data)
    assert len(memory_storage.keys_for(org_id)) == 1
    flow.ok("upload")

    # ---------------------------------------------------------------- 3. dataset ingest (Celery eager on commit)
    with django_capture_on_commit_callbacks(execute=True):
        created = client.post(
            f"{base}/datasets",
            {"name": "Sales", "upload_id": up.json()["id"], "classification": "synthetic", "attest": True},
            format="json",
        )
    assert created.status_code == 202, created.content[:500]
    accepted = created.json()
    dataset_id = accepted["dataset"]["id"]
    job = client.get(f"{base}/jobs/{accepted['job_id']}")
    assert job.status_code == 200 and job.json()["status"] == "succeeded", job.content[:500]
    dataset = client.get(f"{base}/datasets/{dataset_id}").json()
    assert dataset["status"] == "ready", dataset.get("last_error")
    assert dataset["classification"] == "synthetic"
    assert dataset["row_count"] == 5020 and dataset["column_count"] == 12
    flow.ok("ingest")

    # ---------------------------------------------------------------- 4. profiling + quality
    columns = client.get(f"{base}/datasets/{dataset_id}/columns").json()
    by_name = {c["name"]: c for c in columns}
    assert set(by_name) >= {"order_id", "order_date", "region", "revenue", "channel"}
    assert by_name["region"]["stats"]["null_pct"] > 0  # the 6 planted empty cells
    top_regions = {str(t.get("value", t) if isinstance(t, dict) else t[0]) for t in
                   by_name["region"]["stats"]["top_values"]}
    assert REGIONS <= top_regions
    assert by_name["revenue"]["data_type"] in ("numeric", "double precision", "bigint")
    quality = client.get(f"{base}/datasets/{dataset_id}/quality").json()
    assert 0 <= quality["score"] <= 100
    assert set(quality["dimensions"]) >= {"completeness", "uniqueness", "validity", "consistency"}
    assert quality["duplicates"]["exact_rows"] == 20  # testdata plants exactly 20 duplicate rows
    assert dataset["quality_score"] is not None
    flow.ok("profiling+quality")

    # ---------------------------------------------------------------- 5. semantic model
    semantics = _semantics(org_id, dataset_id)
    if semantics is None:
        flow.absent("semantic model", "apps.semantic.services.dataset_semantics")
    else:
        assert str(semantics.dataset_id) == str(dataset_id)
        assert semantics.classification == "synthetic"
        (logical,) = list(semantics.tables)
        assert "." in semantics.tables[logical]  # physical "schema.table"
        assert set(semantics.columns_by_table[logical]) >= {"region", "revenue", "order_date"}
        roles = {c["name"]: c.get("role") for c in semantics.columns_meta.get(logical, ())}
        assert roles.get("revenue") in (None, "measure") and roles.get("region") in (None, "dimension")
        flow.ok("semantic model")

    # ---------------------------------------------------------------- 6. BI conversation + question (SSE)
    if not route_exists(f"{base}/bi/conversations"):
        flow.absent("bi conversation", "POST /orgs/{org}/bi/conversations")
        flow.finish()
    conv = client.post(f"{base}/bi/conversations", {"dataset_id": dataset_id, "title": "E2E"}, format="json")
    assert conv.status_code == 201, conv.content[:500]
    conv_id = conv.json()["id"]
    resp = client.post(f"{base}/bi/conversations/{conv_id}/messages", {"question": QUESTION}, format="json",
                       HTTP_ACCEPT="text/event-stream")
    assert resp.status_code == 200, getattr(resp, "content", b"")[:500]
    assert resp["Content-Type"].startswith("text/event-stream")
    events = parse_sse(read_stream(resp))
    named = events_by_name(events)
    assert "error" not in named, named.get("error")
    stages = [e["stage"] for e in named.get("status", [])]
    assert stages and stages[0] in ("authorizing", "resolving"), stages
    (sql_event,) = named["sql"]
    assert sql_event["source"] == "builder"
    assert sql_event["raw_sql"] and sql_event["executed_sql"] and sql_event["query_audit_id"]
    assert "region" in sql_event["executed_sql"].lower()
    (result,) = named["result"]
    names = _column_names(result["columns"])
    rows = [tuple(r) for r in result["rows"]]
    assert {r[0] for r in rows if r[0] is not None} == REGIONS
    assert compare(expected_result(REGION_CHECK), names, rows) == [], rows
    assert result["truncated"] is False and result["row_count"] == len(rows)
    chart = result.get("chart") or {}
    assert chart.get("type") in ("bar", "pie") and chart.get("x") == "region", chart
    assert named.get("insight"), "expected at least one insight event"
    assert all(i.get("text") for i in named["insight"])
    (done,) = named["done"]
    message_id = done["message_id"]
    assert events[-1].event == "done"
    # the builder answered: no NL->SQL prompt left the process; whatever did (narration) passed the real gate
    assert "nl2sql.generate" not in [c.prompt_key for c in gated_fake_gemini.calls]
    assert gated_fake_gemini.blocked == []
    assert network_spy.external == []
    flow.ok("bi question (SSE)")

    messages = client.get(f"{base}/bi/conversations/{conv_id}/messages")
    assert messages.status_code == 200
    items = messages.json()["items"] if isinstance(messages.json(), dict) else messages.json()
    assistant = [m for m in items if m["role"] == "assistant"]
    assert assistant and assistant[-1]["id"] == message_id
    assert assistant[-1]["sql"]["source"] == "builder" and assistant[-1]["chart"]["x"] == "region"

    # JSON (synchronous) variant of the same endpoint
    sync = client.post(f"{base}/bi/conversations/{conv_id}/messages", {"question": "top 3 regions by revenue"},
                       format="json", HTTP_ACCEPT="application/json")
    assert sync.status_code == 200, sync.content[:500]
    msg = sync.json()
    assert msg["role"] == "assistant" and msg["sql"]["source"] == "builder"
    ranking = expected_result({**REGION_CHECK, "top": 3, "order": "desc"})
    assert compare(ranking, _column_names(msg["result"]["columns"]),
                   [tuple(r) for r in msg["result"]["rows"]]) == []
    flow.ok("bi question (JSON)")

    # ---------------------------------------------------------------- 7. query audit
    audit_id = str(sql_event["query_audit_id"])
    model = _audit_model()
    if model is not None:
        from apps.core.tenancy import tenant_context
        from testing.db import clear_db_tenant

        with tenant_context(org_id):
            row = model.objects.filter(id=audit_id).first()
            assert row is not None, "no BiQueryAudit row for the sql event's query_audit_id"
            assert getattr(row, "executed_sql", None) and getattr(row, "reject_code", None) in (None, "")
        clear_db_tenant()
        flow.ok("query audit row")
    else:
        flow.absent("query audit model", "apps.bi BiQueryAudit")
    history_url = f"{base}/bi/query-history"
    if route_exists(history_url):
        history = client.get(history_url)
        assert history.status_code == 200
        entries = history.json()["items"] if isinstance(history.json(), dict) else history.json()
        mine = [e for e in entries if str(e["id"]) == audit_id]
        assert mine and mine[0]["question"] == QUESTION and mine[0]["source"] == "builder", entries[:3]
        assert mine[0]["executed_sql"] and mine[0]["row_count"] == len(rows)
        flow.ok("query history")
    else:
        flow.absent("query history", "GET /orgs/{org}/bi/query-history")

    # ---------------------------------------------------------------- 8. a second org sees none of it
    other, other_org = _signup("e2e-intruder@example.test", "E2E Intruder")
    other_base = f"/api/v1/orgs/{other_org['slug']}"
    for path in (
        f"{base}/bi/conversations/{conv_id}/messages",          # A's slug, not a member
        f"{other_base}/bi/conversations/{conv_id}/messages",    # own slug, A's id (IDOR)
        f"{base}/datasets/{dataset_id}",
        f"{other_base}/datasets/{dataset_id}",
        f"{base}/bi/query-history",
        f"{base}/uploads/{up.json()['id']}",
        f"{other_base}/uploads/{up.json()['id']}",
    ):
        r = other.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
    if route_exists(f"{other_base}/bi/query-history"):
        own = other.get(f"{other_base}/bi/query-history").content.decode()
        assert audit_id not in own and QUESTION not in own
    leaked = other.post(f"{other_base}/bi/conversations", {"dataset_id": dataset_id}, format="json")
    assert leaked.status_code in (400, 404), "a conversation on another org's dataset must not be created"
    flow.ok("cross-org isolation")
    flow.finish()
