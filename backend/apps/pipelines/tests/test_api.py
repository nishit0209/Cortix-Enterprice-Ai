"""Pipelines API + runner + scheduler (DB-backed: real Postgres under RLS + nexora_wh; storage faked).

Source datasets go through the real upload -> ingest path; runs execute eagerly once the request "commits"
(django_capture_on_commit_callbacks), including the output ingestion and the finalize step.
"""

import datetime as dt
import uuid

import pytest
from django.core import mail
from django.utils import timezone

from apps.core.models import AuditEvent, Job
from apps.datasets.tests.helpers import csv_bytes, ingest
from apps.pipelines import runner, scheduling
from apps.pipelines.engine import RunFailure
from apps.pipelines.models import Pipeline, PipelineRun, PipelineRunLog, PipelineSchedule

pytestmark = pytest.mark.django_db

ROWS = [["Order ID", "City", "Amount", "Qty"],
        *[[1000 + i, [" Pune", "Mumbai ", "Delhi", "pune"][i % 4], f"{(i * 37) % 500}.25", i % 5 if i % 7 else ""]
          for i in range(40)]]
CITIES = [["City", "State"], ["Pune", "MH"], ["Mumbai", "MH"], ["Delhi", "DL"]]


def url(world, path=""):
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def env(two_orgs, api_client_for, fake_redis, fake_storage, django_capture_on_commit_callbacks):
    a, b = two_orgs
    client = api_client_for(a.owner)
    _resp, orders = ingest(client, a, "orders.csv", csv_bytes(ROWS), django_capture_on_commit_callbacks, name="Orders")
    _resp, cities = ingest(client, a, "cities.csv", csv_bytes(CITIES), django_capture_on_commit_callbacks,
                           name="Cities")
    assert orders["status"] == "ready" and cities["status"] == "ready"
    return a, b, client, orders, cities


def dag(orders_id, *steps, dest=None):
    nodes = [{"id": "src", "type": "source.dataset", "config": {"dataset_id": orders_id}}, *steps,
             dest or {"id": "dst", "type": "destination.dataset", "config": {"name": "Clean orders"}}]
    return {"nodes": nodes, "edges": [{"from": x["id"], "to": y["id"]} for x, y in zip(nodes, nodes[1:])]}


def clean_steps(cities_id):
    return [
        {"id": "trim", "type": "trim_whitespace", "config": {"columns": ["city"]}},
        {"id": "case", "type": "change_case", "config": {"columns": ["city"], "case": "title"}},
        {"id": "dedup", "type": "deduplicate", "config": {"subset": ["order_id"]}},
        {"id": "fill", "type": "fill_null", "config": {"columns": ["qty"], "strategy": "zero"}},
        {"id": "join", "type": "join", "config": {"dataset_id": cities_id, "how": "left",
                                                   "on": [{"left": "city", "right": "city"}]}},
        {"id": "big", "type": "filter", "config": {"conditions": [{"column": "amount", "op": "gte", "value": 100}]}},
        {"id": "tax", "type": "derive", "config": {"name": "amount_with_tax", "expression": {
            "op": "round", "args": [{"op": "mul", "args": [{"col": "amount"}, {"lit": 1.18}]}, {"lit": 2}]}}},
        {"id": "sort", "type": "sort", "config": {"by": [{"column": "amount", "descending": True}]}},
    ]


def create_pipeline(client, world, body):
    resp = client.post(url(world, "pipelines"), body, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def run_now(client, world, pid, capture):
    with capture(execute=True):
        resp = client.post(url(world, f"pipelines/{pid}/runs"))
    assert resp.status_code == 202, resp.content
    return resp.json()


# --- CRUD + validation ------------------------------------------------------------------------------------------
def test_create_validates_dag_per_node(env):
    a, _b, client, orders, cities = env
    bad = dag(orders["id"], {"id": "pick", "type": "select_columns", "config": {"columns": ["nope"]}},
              {"id": "evil", "type": "python", "config": {"code": "import os"}})
    resp = client.post(url(a, "pipelines"), {"name": "Bad", "dag": bad}, format="json")
    assert resp.status_code == 400
    fields = {(e["field"], e["code"]) for e in resp.json()["errors"]}
    assert ("dag.nodes.evil.type", "unknown_node_type") in fields
    missing = dag(str(uuid.uuid4()))
    resp2 = client.post(url(a, "pipelines"), {"name": "Missing", "dag": missing}, format="json")
    assert ("dag.nodes.src.config.dataset_id", "dataset_not_found") in {(e["field"], e["code"])
                                                                        for e in resp2.json()["errors"]}
    cols = client.post(url(a, "pipelines"), {"name": "Cols", "dag": dag(orders["id"], {
        "id": "pick", "type": "select_columns", "config": {"columns": ["nope"]}})}, format="json")
    assert ("dag.nodes.pick.config.columns", "unknown_column") in {(e["field"], e["code"])
                                                                   for e in cols.json()["errors"]}


def test_crud_versions_and_validate_endpoint(env):
    a, _b, client, orders, cities = env
    body = create_pipeline(client, a, {"name": "Orders clean", "dag": dag(orders["id"], *clean_steps(cities["id"]))})
    assert body["status"] == "active" and body["version"] == 1 and body["last_run"] is None
    assert body["dag"]["nodes"][0]["type"] == "source.dataset"
    pid = body["id"]
    patched = client.patch(url(a, f"pipelines/{pid}"), {"dag": dag(orders["id"])}, format="json").json()
    assert patched["version"] == 2
    same = client.patch(url(a, f"pipelines/{pid}"), {"dag": dag(orders["id"])}, format="json").json()
    assert same["version"] == 2  # identical DAG -> no new version
    versions = client.get(url(a, f"pipelines/{pid}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1] and versions[0]["current"]
    v = client.post(url(a, "pipelines/validate"), {"dag": dag(orders["id"], {
        "id": "c", "type": "cast", "config": {"columns": {"qty": "integer"}}})}, format="json").json()
    assert v["valid"] and v["schemas"]["c"][0]["name"] == "order_id"
    listed = client.get(url(a, "pipelines")).json()["items"]
    assert [p["id"] for p in listed] == [pid]
    assert client.post(url(a, "pipelines"), {"name": "Orders clean"}, format="json").status_code == 409
    draft = create_pipeline(client, a, {"name": "Draft only"})
    assert draft["status"] == "draft" and draft["dag"] is None
    assert client.post(url(a, f"pipelines/{draft['id']}/runs")).status_code == 409


# --- runs ----------------------------------------------------------------------------------------------------------
def test_run_end_to_end(env, django_capture_on_commit_callbacks):
    from apps.lineage import services as lineage

    a, _b, client, orders, cities = env
    pid = create_pipeline(client, a, {"name": "Orders clean",
                                      "dag": dag(orders["id"], *clean_steps(cities["id"]))})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    assert accepted["events_url"].endswith("/events")
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "succeeded", run
    assert run["attempt"] == 1 and run["rows_in"] == 40 and run["duration_ms"] is not None
    assert run["rows_out"] == run["quality_summary"]["row_count"] > 0
    assert run["quality_summary"]["score"] is not None and run["quality_summary"]["current"] is True
    by = {s["id"]: s for s in run["steps"]}
    assert set(by) == {"src", "trim", "case", "dedup", "fill", "join", "big", "tax", "sort", "dst"}
    assert all(s["status"] == "succeeded" for s in run["steps"])
    assert by["src"]["rows_out"] == 40 and by["big"]["rows_out"] < by["big"]["rows_in"]
    assert any("Step join finished" in log["message"] for log in run["logs"])
    out = client.get(url(a, f"datasets/{run['output_dataset_id']}")).json()
    assert out["status"] == "ready" and out["row_count"] == run["rows_out"] and out["tags"] == ["pipeline"]
    cols = [c["name"] for c in client.get(url(a, f"datasets/{run['output_dataset_id']}/columns")).json()]
    assert "state" in cols and "amount_with_tax" in cols
    preview = client.get(url(a, f"datasets/{run['output_dataset_id']}/preview?limit=5")).json()
    cities_out = {r[cols.index("city")] for r in preview["rows"]}
    assert cities_out <= {"Pune", "Mumbai", "Delhi"}
    with a.tenant():
        graph = lineage.graph("dataset", run["output_dataset_id"], depth=4)
        types = {n["type"] for n in graph["nodes"]}
        assert {"pipeline", "pipeline_run", "dataset_version", "dataset"} <= types
        assert any(n["id"] == pid for n in graph["nodes"])
        assert AuditEvent.objects.filter(action="pipeline.run.succeeded", target_id=pid).exists()
        assert Job.objects.get(id=accepted["job_id"]).status == "succeeded"
    stats = client.get(url(a, "data-stats")).json()
    assert stats["pipelines_active"] == 1 and stats["runs_7d"] == {"succeeded": 1, "failed": 0, "total": 1}
    detail = client.get(url(a, f"pipelines/{pid}")).json()
    assert detail["last_run"]["status"] == "succeeded" and detail["output_dataset_id"] == run["output_dataset_id"]

    # a second run writes a NEW VERSION of the same output dataset
    second = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run2 = client.get(url(a, f"pipeline-runs/{second['run']['id']}")).json()
    assert run2["status"] == "succeeded" and run2["output_dataset_id"] == run["output_dataset_id"]
    assert len(client.get(url(a, f"datasets/{run['output_dataset_id']}/versions")).json()) == 2
    runs = client.get(url(a, f"pipelines/{pid}/runs")).json()["items"]
    assert [r["number"] for r in runs] == [2, 1]


def test_one_active_run_per_pipeline(env):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Single", "dag": dag(orders["id"])})["id"]
    first = client.post(url(a, f"pipelines/{pid}/runs"))  # not executed (callbacks not run): stays queued
    assert first.status_code == 202
    second = client.post(url(a, f"pipelines/{pid}/runs"))
    assert second.status_code == 409 and second.json()["code"] == "pipeline_run_active"
    cancelled = client.post(url(a, f"pipeline-runs/{first.json()['run']['id']}/cancel"))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    # the (late) worker delivery for a cancelled run is a no-op
    assert runner.execute_run(org_id=str(a.org.id), run_id=first.json()["run"]["id"]) is None
    assert client.post(url(a, f"pipeline-runs/{first.json()['run']['id']}/cancel")).status_code == 409


def test_non_retryable_failure_fails_fast_audits_and_notifies(env, django_capture_on_commit_callbacks):
    a, _b, client, orders, _c = env
    bad = dag(orders["id"], {"id": "c", "type": "cast", "config": {"columns": {"city": "integer"}, "on_error": "fail"}})
    pid = create_pipeline(client, a, {"name": "Strict cast", "dag": bad, "max_retries": 3})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "failed" and run["error_code"] == "cast_failed" and run["attempt"] == 1
    assert run["error_detail"]["node"] == "c"
    by = {s["id"]: s for s in run["steps"]}
    assert by["c"]["status"] == "failed" and by["c"]["error"]["code"] == "cast_failed"
    assert by["dst"]["status"] == "skipped"
    assert any(log["level"] == "error" for log in run["logs"])
    with a.tenant():
        job = Job.objects.get(id=accepted["job_id"])
        assert job.status == "failed" and job.error["code"] == "cast_failed"
        assert AuditEvent.objects.filter(action="pipeline.run.failed", target_id=pid).exists()
    assert len(mail.outbox) == 1 and "Strict cast" in mail.outbox[0].subject
    assert mail.outbox[0].to == [a.owner.email]
    assert client.get(url(a, "data-stats")).json()["runs_7d"]["failed"] == 1


def test_transient_failure_retries_with_backoff_then_succeeds(env, django_capture_on_commit_callbacks, monkeypatch):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Flaky", "dag": dag(orders["id"]), "max_retries": 2,
                                      "retry_backoff_s": 1})["id"]
    original = runner.RunContext.load_dataset
    calls = {"n": 0}

    def flaky(self, dataset_id, limit, *, role="join input"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RunFailure("connector_timeout", "The source did not answer in time.")
        return original(self, dataset_id, limit, role=role)

    monkeypatch.setattr(runner.RunContext, "load_dataset", flaky)
    countdowns = []
    from apps.pipelines import tasks

    real_apply = tasks.execute_pipeline_run.apply_async

    def spy(*args, **kwargs):
        countdowns.append(kwargs.get("countdown"))
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(tasks.execute_pipeline_run, "apply_async", spy)
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "succeeded" and run["attempt"] == 2
    assert [x["error_code"] for x in run["attempts"]] == ["connector_timeout", None]
    assert countdowns[0] is None and 1 <= countdowns[1] <= 2  # first enqueue, then backoff 1s * 2^0 (+ jitter)
    assert any("Retrying in" in log["message"] for log in run["logs"])
    with a.tenant():
        assert AuditEvent.objects.filter(action="pipeline.run.retry_scheduled", target_id=pid).exists()


def test_retries_are_bounded(env, django_capture_on_commit_callbacks, monkeypatch):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Always flaky", "dag": dag(orders["id"]), "max_retries": 2,
                                      "retry_backoff_s": 1})["id"]

    def always(self, *a, **k):
        raise RunFailure("warehouse_unavailable", "The warehouse is unavailable.")

    original = runner.RunContext.load_dataset
    monkeypatch.setattr(runner.RunContext, "load_dataset", always)
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "failed" and run["attempt"] == 3 and run["max_attempts"] == 3
    monkeypatch.setattr(runner.RunContext, "load_dataset", original)  # undo() would also drop fixture patches
    assert [x["error_code"] for x in run["attempts"]] == ["warehouse_unavailable"] * 3

    # manual retry creates a NEW run linked to the failed one, on the same version
    with django_capture_on_commit_callbacks(execute=True):
        retried = client.post(url(a, f"pipeline-runs/{run['id']}/retry"))
    assert retried.status_code == 202
    new = client.get(url(a, f"pipeline-runs/{retried.json()['run']['id']}")).json()
    assert new["trigger"] == "retry" and new["parent_run_id"] == run["id"] and new["status"] == "succeeded"
    assert client.post(url(a, f"pipeline-runs/{new['id']}/retry")).status_code == 409


def test_blocking_quality_failure_fails_without_retry(env, django_capture_on_commit_callbacks):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Gate", "dag": dag(orders["id"]), "max_retries": 3})["id"]
    first = run_now(client, a, pid, django_capture_on_commit_callbacks)
    out_id = client.get(url(a, f"pipeline-runs/{first['run']['id']}")).json()["output_dataset_id"]
    rule = client.post(url(a, f"datasets/{out_id}/quality-rules"), {
        "rule_type": "row_count", "params": {"min": 100000}, "severity": "error", "is_blocking": True},
        format="json")
    assert rule.status_code == 201, rule.content
    second = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{second['run']['id']}")).json()
    assert run["status"] == "failed" and run["error_code"] == "quality_blocking_failure" and run["attempt"] == 1
    assert run["quality_summary"]["blocking_failures"]


def test_preview_runs_on_200_rows_without_writes(env):
    from apps.datasets.models import Dataset, Upload

    a, _b, client, orders, cities = env
    pid = create_pipeline(client, a, {"name": "Preview me", "dag": dag(orders["id"], *clean_steps(cities["id"]))})["id"]
    with a.tenant():
        before = (Dataset.objects.count(), Upload.objects.count())
    resp = client.post(url(a, f"pipelines/{pid}/preview"), {"limit": 5}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["ok"] and body["row_count"] <= 5 and body["source_rows"] == 40
    assert [c["name"] for c in body["columns"]][-2:] == ["state", "amount_with_tax"]
    assert {s["id"] for s in body["steps"]} >= {"src", "join", "sort"}
    other = client.post(url(a, f"pipelines/{pid}/preview"), {"dag": dag(orders["id"], {
        "id": "agg", "type": "aggregate", "config": {"group_by": ["city"], "aggregations": [{"func": "count"}]}})},
        format="json").json()
    assert [c["name"] for c in other["columns"]] == ["city", "row_count"]
    with a.tenant():
        assert (Dataset.objects.count(), Upload.objects.count()) == before
        assert not PipelineRun.objects.exists()


def test_schedule_and_dispatcher(env, django_capture_on_commit_callbacks):
    from testing.db import db_tenant

    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Nightly", "dag": dag(orders["id"])})["id"]
    bad = client.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "* * * * *"}, format="json")
    assert bad.status_code == 400
    resp = client.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "30 2 * * *", "timezone": "Asia/Kolkata"},
                      format="json")
    assert resp.status_code == 200 and resp.json()["next_run_at"]
    assert client.get(url(a, f"pipelines/{pid}")).json()["schedule"]["cron"] == "30 2 * * *"
    past = timezone.now() - dt.timedelta(minutes=1)
    with a.tenant():
        sched = PipelineSchedule.objects.get(pipeline_id=pid)
        PipelineSchedule.objects.filter(id=sched.id).update(next_run_at=past)
    with db_tenant(a.org.id):
        due = scheduling.scan_due(timezone.now(), using="default")
    assert (a.org.id, sched.id) in due
    with django_capture_on_commit_callbacks(execute=True):
        outcome = scheduling.fire_schedule(a.org.id, sched.id)
    assert outcome == "enqueued"
    with a.tenant():
        sched.refresh_from_db()
        assert sched.next_run_at > timezone.now() and sched.last_run_id
        run = PipelineRun.objects.get(id=sched.last_run_id)
        assert run.trigger == "schedule" and run.status == "succeeded"
    assert scheduling.fire_schedule(a.org.id, sched.id) == "not_due"
    # paused pipelines skip their ticks but keep advancing
    client.patch(url(a, f"pipelines/{pid}"), {"status": "paused"}, format="json")
    with a.tenant():
        PipelineSchedule.objects.filter(id=sched.id).update(next_run_at=past)
    assert scheduling.fire_schedule(a.org.id, sched.id) == "inactive"
    assert client.delete(url(a, f"pipelines/{pid}/schedule")).status_code == 204
    assert client.get(url(a, f"pipelines/{pid}/schedule")).json() is None


def test_scheduled_tick_skips_while_a_run_is_active(env):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Busy", "dag": dag(orders["id"])})["id"]
    client.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "0 * * * *"}, format="json")
    client.post(url(a, f"pipelines/{pid}/runs"))  # queued, never executed here
    with a.tenant():
        sched = PipelineSchedule.objects.get(pipeline_id=pid)
        PipelineSchedule.objects.filter(id=sched.id).update(next_run_at=timezone.now() - dt.timedelta(minutes=1))
    assert scheduling.fire_schedule(a.org.id, sched.id) == "skipped_active"
    with a.tenant():
        assert PipelineRun.objects.filter(pipeline_id=pid).count() == 1
        assert AuditEvent.objects.filter(action="pipeline.schedule.skipped", target_id=pid).exists()


def test_stuck_runs_are_reaped(env, django_capture_on_commit_callbacks, settings):
    a, _b, client, orders, _c = env
    settings.PIPELINE_HEARTBEAT_TIMEOUT_S = 60
    pid = create_pipeline(client, a, {"name": "Lost worker", "dag": dag(orders["id"]), "max_retries": 0})["id"]
    run_id = client.post(url(a, f"pipelines/{pid}/runs")).json()["run"]["id"]
    with a.tenant():
        PipelineRun.objects.filter(id=run_id).update(status="running", started_at=timezone.now(),
                                                     heartbeat_at=timezone.now() - dt.timedelta(minutes=5))
    assert scheduling.reap_stuck_run(a.org.id, uuid.UUID(run_id)) == "reaped"
    with a.tenant():
        run = PipelineRun.objects.get(id=run_id)
        assert run.status == "failed" and run.error_code == "worker_lost"


def test_archive(env):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Old", "dag": dag(orders["id"])})["id"]
    client.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "0 3 * * *"}, format="json")
    assert client.delete(url(a, f"pipelines/{pid}")).status_code == 204
    assert client.get(url(a, f"pipelines/{pid}")).status_code == 404
    assert client.get(url(a, "pipelines")).json()["items"] == []
    with a.tenant():
        assert Pipeline.objects.get(id=pid).status == "archived"
        assert PipelineSchedule.objects.get(pipeline_id=pid).enabled is False


def test_run_logs_are_append_only(env, django_capture_on_commit_callbacks):
    from django.db import DatabaseError, transaction

    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Logs", "dag": dag(orders["id"])})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    logs = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}/logs")).json()["items"]
    assert logs and logs[0]["message"].startswith("Run #1 queued")
    with a.tenant():
        with pytest.raises(DatabaseError), transaction.atomic():
            PipelineRunLog.objects.filter(run_id=accepted["run"]["id"]).update(message="tampered")


def test_permissions(env, api_client_for, django_capture_on_commit_callbacks):
    a, _b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Perms", "dag": dag(orders["id"])})["id"]
    analyst = api_client_for(a.analyst)
    assert analyst.get(url(a, "pipelines")).status_code == 200
    assert analyst.get(url(a, f"pipelines/{pid}/runs")).status_code == 200
    assert analyst.post(url(a, "pipelines"), {"name": "x"}, format="json").status_code == 403
    assert analyst.post(url(a, f"pipelines/{pid}/runs")).status_code == 403
    assert analyst.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "0 1 * * *"}, format="json").status_code == 403
    assert analyst.post(url(a, f"pipelines/{pid}/preview"), {}, format="json").status_code == 403
    viewer = api_client_for(a.viewer)
    assert viewer.get(url(a, "pipelines")).status_code == 403
    engineer = api_client_for(a.user("data_engineer"))
    with django_capture_on_commit_callbacks(execute=True):
        assert engineer.post(url(a, f"pipelines/{pid}/runs")).status_code == 202


def test_cross_org_is_404(env, api_client_for, django_capture_on_commit_callbacks):
    a, b, client, orders, _c = env
    pid = create_pipeline(client, a, {"name": "Private", "dag": dag(orders["id"])})["id"]
    run_id = run_now(client, a, pid, django_capture_on_commit_callbacks)["run"]["id"]
    other = api_client_for(b.owner)
    for method, path in (("get", f"pipelines/{pid}"), ("patch", f"pipelines/{pid}"), ("delete", f"pipelines/{pid}"),
                         ("get", f"pipelines/{pid}/runs"), ("post", f"pipelines/{pid}/runs"),
                         ("put", f"pipelines/{pid}/schedule"), ("post", f"pipelines/{pid}/preview"),
                         ("get", f"pipelines/{pid}/versions"), ("get", f"pipeline-runs/{run_id}"),
                         ("get", f"pipeline-runs/{run_id}/logs"), ("post", f"pipeline-runs/{run_id}/retry"),
                         ("post", f"pipeline-runs/{run_id}/cancel")):
        resp = getattr(other, method)(url(b, path), {"cron": "0 1 * * *"} if method == "put" else {}, format="json")
        assert resp.status_code == 404, (method, path, resp.status_code)
    assert other.get(url(b, "pipelines")).json()["items"] == []
    assert other.get(url(b, "pipeline-runs")).json()["items"] == []
    # org B cannot build a pipeline on org A's dataset either
    stolen = other.post(url(b, "pipelines"), {"name": "Steal", "dag": dag(orders["id"])}, format="json")
    assert stolen.status_code == 400
    assert ("dag.nodes.src.config.dataset_id", "dataset_not_found") in {(e["field"], e["code"])
                                                                        for e in stolen.json()["errors"]}
