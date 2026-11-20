"""W3 pipelines (DB-backed: real Postgres under RLS + nexora_wh; storage faked, notify recorded).

Covers the frontend W2 requests (detail validate = dry run with per-node errors, preview `node_id` + `limit`,
schedule `is_active`, UI step names, rule counts), owner notifications, data-stats KPIs and the dispatcher paths the
in-process thread runner relies on (due retries, lost queued runs, finalize on ingest).
"""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from apps.datasets.tests.helpers import csv_bytes, ingest
from apps.pipelines import runner, scheduling
from apps.pipelines.models import PipelineRun

from .test_api import CITIES, ROWS, clean_steps, create_pipeline, dag, run_now, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def env(two_orgs, api_client_for, fake_redis, fake_storage, django_capture_on_commit_callbacks):
    """Org A with two ready source datasets (same as the W2 suite's fixture)."""
    a, b = two_orgs
    client = api_client_for(a.owner)
    _resp, orders = ingest(client, a, "orders.csv", csv_bytes(ROWS), django_capture_on_commit_callbacks, name="Orders")
    _resp, cities = ingest(client, a, "cities.csv", csv_bytes(CITIES), django_capture_on_commit_callbacks,
                           name="Cities")
    assert orders["status"] == "ready" and cities["status"] == "ready"
    return a, b, client, orders, cities

STRICT_CAST = {"id": "c", "type": "cast", "config": {"columns": {"city": "integer"}, "on_error": "fail"}}


# --- validate / dry run ----------------------------------------------------------------------------------------
def test_validate_detail_returns_per_node_errors(env):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Checked", "dag": dag(orders["id"])})["id"]
    bad = dag(orders["id"], {"id": "pick", "type": "select_columns", "config": {"columns": ["nope"]}})
    resp = client.post(url(a, f"pipelines/{pid}/validate"), {"dag": bad}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["valid"] is False and body["ok"] is False and body["rows"] == [] and body["row_count"] == 0
    pick = [e for e in body["errors"] if e["node_id"] == "pick"]
    assert pick and pick[0]["field"] == "nodes.pick.config.columns" and pick[0]["code"] == "unknown_column"
    assert pick[0]["node"] == "pick"  # W2 name kept


def test_validate_detail_dry_runs_the_current_version_on_a_sample(env):
    a, _b, client, orders, cities = env
    pid = create_pipeline(client, a, {"name": "Sampled", "dag": dag(orders["id"], *clean_steps(cities["id"]))})["id"]
    body = client.post(url(a, f"pipelines/{pid}/validate"), {"node_id": "trim", "limit": 3}, format="json").json()
    assert body["valid"] is True and body["ok"] is True and body["errors"] == []
    assert body["node_id"] == "trim" and body["row_count"] == 3 and len(body["rows"]) == 3
    assert body["source_rows"] == 40 and body["schemas"]["trim"]
    steps = {s["step_key"]: s["status"] for s in body["steps"]}
    assert steps["src"] == steps["trim"] == "succeeded" and steps["case"] == steps["sort"] == "skipped"
    city = [c["name"] for c in body["columns"]].index("city")
    assert all(row[city] == row[city].strip() for row in body["rows"])


def test_a_step_failing_on_the_sample_is_pinned_on_its_node(env):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Strict", "dag": dag(orders["id"], STRICT_CAST)})["id"]
    body = client.post(url(a, f"pipelines/{pid}/validate"), {}, format="json").json()
    assert body["valid"] is True and body["ok"] is False
    assert body["runtime_error"]["code"] == "cast_failed"
    assert body["errors"] == [{"node_id": "c", "node": "c", "field": "nodes.c.data", "code": "cast_failed",
                               "message": body["runtime_error"]["message"]}]


def test_collection_validate_names_node_ids(env):
    a, _b, client, orders, _cities = env
    bad = dag(orders["id"], {"id": "evil", "type": "python", "config": {}})
    body = client.post(url(a, "pipelines/validate"), {"dag": bad}, format="json").json()
    evil = [e for e in body["errors"] if e["node_id"] == "evil"]
    assert body["valid"] is False and evil and all(e["field"].startswith("nodes.evil") for e in evil)


def test_validate_detail_unknown_node_and_permissions(env, api_client_for):
    a, b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Guarded", "dag": dag(orders["id"])})["id"]
    assert client.post(url(a, f"pipelines/{pid}/validate"), {"node_id": "ghost"}, format="json").status_code == 400
    viewer = api_client_for(a.analyst)  # pipelines.view only
    assert viewer.post(url(a, f"pipelines/{pid}/validate"), {}, format="json").status_code == 403
    other = api_client_for(b.owner)
    assert other.post(url(b, f"pipelines/{pid}/validate"), {}, format="json").status_code == 404


def test_builder_style_join_is_stored_as_drawn_and_runs(env, django_capture_on_commit_callbacks):
    """The builder wires the right dataset of a join as a second source on the join's right handle."""
    a, _b, client, orders, cities = env
    nodes = [
        {"id": "src", "type": "source.dataset", "config": {"dataset_id": orders["id"]}, "position": {"x": 0, "y": 0}},
        {"id": "trim", "type": "trim_whitespace", "config": {"columns": ["city"]}},
        {"id": "case", "type": "change_case", "config": {"columns": ["city"], "case": "title"}},
        {"id": "cities", "type": "source.dataset", "config": {"dataset_id": cities["id"]}},
        {"id": "join", "type": "join", "config": {"how": "left", "on": [{"left": "city", "right": "city"}]}},
        {"id": "dst", "type": "destination.dataset", "config": {"name": "Joined orders"}},
    ]
    edges = [{"from": "src", "to": "trim"}, {"from": "trim", "to": "case"},
             {"from": "case", "to": "join", "port": "left"}, {"from": "cities", "to": "join", "port": "right"},
             {"from": "join", "to": "dst"}]
    body = create_pipeline(client, a, {"name": "Builder join", "dag": {"nodes": nodes, "edges": edges}})
    assert [n["id"] for n in body["dag"]["nodes"]] == ["src", "trim", "case", "cities", "join", "dst"]
    assert {"from": "cities", "to": "join", "port": "right"} in body["dag"]["edges"]
    accepted = run_now(client, a, body["id"], django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "succeeded", run
    assert [s["id"] for s in run["steps"]] == ["src", "trim", "case", "join", "dst"]
    cols = [c["name"] for c in client.get(url(a, f"datasets/{run['output_dataset_id']}/columns")).json()]
    assert "state" in cols
    refused = client.post(url(a, f"pipelines/{body['id']}/validate"), {"node_id": "cities"}, format="json")
    assert refused.status_code == 400 and "preview the join instead" in refused.content.decode()


def test_a_dag_written_by_the_builder_is_accepted_and_runs(env, django_capture_on_commit_callbacks):
    """Node types and config keys exactly as frontend/src/lib/pipelines/catalog.ts + inspector.tsx write them."""
    a, _b, client, orders, _cities = env
    nodes = [
        {"id": "source_1", "type": "source", "config": {"dataset_id": orders["id"]}, "position": {"x": 0, "y": 0}},
        {"id": "trim_1", "type": "trim_whitespace", "config": {"columns": ["city"]}},
        {"id": "cast_1", "type": "cast", "config": {"casts": [{"column": "qty", "to": "bigint"}], "on_error": "null"}},
        {"id": "filter_1", "type": "filter", "config": {"conditions": [{"column": "amount", "op": ">=", "value": 100}],
                                                       "mode": "all"}},
        {"id": "derive_1", "type": "derive", "config": {"name": "amount_with_tax",
                                                       "expression": "round(amount * 1.18, 2)"}},
        {"id": "dedup_1", "type": "deduplicate", "config": {"keys": ["order_id"]}},
        {"id": "sort_1", "type": "sort", "config": {"by": [{"column": "amount", "desc": True}]}},
        {"id": "out_1", "type": "destination", "config": {"dataset_name": "orders_builder", "mode": "new_version"}},
    ]
    dag_body = {"nodes": nodes, "edges": [{"from": x["id"], "to": y["id"]} for x, y in zip(nodes, nodes[1:])]}
    body = create_pipeline(client, a, {"name": "From the builder", "dag": dag_body})
    assert body["dag"]["nodes"][0]["type"] == "source"  # stored as the builder wrote it
    accepted = run_now(client, a, body["id"], django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "succeeded", run
    assert client.get(url(a, f"datasets/{run['output_dataset_id']}")).json()["name"] == "orders_builder"
    cols = [c["name"] for c in client.get(url(a, f"datasets/{run['output_dataset_id']}/columns")).json()]
    assert "amount_with_tax" in cols
    upsert = {**dag_body, "nodes": [*nodes[:-1], {**nodes[-1], "config": {"dataset_name": "x", "mode": "upsert"}}]}
    errors = client.post(url(a, "pipelines/validate"), {"dag": upsert}, format="json").json()["errors"]
    assert [e["code"] for e in errors if e["node_id"] == "out_1"] == ["unsupported_mode"]


# --- preview ---------------------------------------------------------------------------------------------------
def test_preview_takes_node_id_and_limit(env):
    a, _b, client, orders, cities = env
    pid = create_pipeline(client, a, {"name": "Peek", "dag": dag(orders["id"], *clean_steps(cities["id"]))})["id"]
    body = client.post(url(a, f"pipelines/{pid}/preview"), {"node_id": "dedup", "limit": 2}, format="json").json()
    assert body["ok"] and body["node_id"] == "dedup" and body["row_count"] == 2
    assert "state" not in [c["name"] for c in body["columns"]]  # the join after `dedup` did not run
    full = client.post(url(a, f"pipelines/{pid}/preview"), {"limit": 2, "node_id": None}, format="json").json()
    assert full["node_id"] is None and "state" in [c["name"] for c in full["columns"]]
    assert client.post(url(a, f"pipelines/{pid}/preview"), {"node_id": "ghost"}, format="json").status_code == 400
    assert client.post(url(a, f"pipelines/{pid}/preview"), {"limit": 500}, format="json").status_code == 400


# --- schedule + run payload ---------------------------------------------------------------------------------------
def test_schedule_accepts_the_ui_is_active_flag(env):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Paused schedule", "dag": dag(orders["id"])})["id"]
    resp = client.put(url(a, f"pipelines/{pid}/schedule"),
                      {"cron": "0 3 * * *", "timezone": "UTC", "is_active": False}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["enabled"] is False and resp.json()["is_active"] is False
    assert client.get(url(a, f"pipelines/{pid}")).json()["schedule"]["is_active"] is False
    resumed = client.put(url(a, f"pipelines/{pid}/schedule"), {"cron": "0 3 * * *", "is_active": True}, format="json")
    assert resumed.json()["is_active"] is True and resumed.json()["next_run_at"]


def test_successful_run_notifies_the_owner_and_carries_the_ui_fields(env, django_capture_on_commit_callbacks,
                                                                      captured_notifications):
    a, _b, client, orders, cities = env
    pid = create_pipeline(client, a, {"name": "Notify me", "dag": dag(orders["id"], *clean_steps(cities["id"]))})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    run = client.get(url(a, f"pipeline-runs/{accepted['run']['id']}")).json()
    assert run["status"] == "succeeded", run
    assert all(s["step_key"] == s["id"] and s["step_type"] == s["type"] for s in run["steps"])
    assert run["quality_summary"]["rules_passed"] is not None and run["quality_summary"]["rules_failed"] is not None
    assert all(line["step_key"] == line["step"] for line in run["logs"])
    # one notification for the run; its output dataset does not notify separately
    assert [n["kind"] for n in captured_notifications] == ["pipeline.run_succeeded"]
    (note,) = captured_notifications
    assert note["user_ids"] == [str(a.owner.pk)] and "Notify me" in note["title"]
    assert note["link"] == f"/o/{a.slug}/data/pipelines/{pid}/runs/{run['id']}"


def test_failed_run_notifies_the_owner(env, django_capture_on_commit_callbacks, captured_notifications):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Strict cast", "dag": dag(orders["id"], STRICT_CAST)})["id"]
    run_now(client, a, pid, django_capture_on_commit_callbacks)
    (note,) = captured_notifications
    assert note["kind"] == "pipeline.run_failed" and note["user_ids"] == [str(a.owner.pk)]
    assert "Strict cast" in note["title"] and "cast_failed" in note["body"]


def test_failure_notification_respects_notify_on_failure(env, django_capture_on_commit_callbacks,
                                                         captured_notifications):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Quiet", "dag": dag(orders["id"], STRICT_CAST),
                                      "notify_on_failure": False})["id"]
    run_now(client, a, pid, django_capture_on_commit_callbacks)
    assert captured_notifications == []


# --- data-stats --------------------------------------------------------------------------------------------------
def test_data_stats_kpis_come_from_the_database(env, django_capture_on_commit_callbacks, api_client_for):
    a, b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "KPI", "dag": dag(orders["id"])})["id"]
    run_now(client, a, pid, django_capture_on_commit_callbacks)
    failing = create_pipeline(client, a, {"name": "KPI fail", "dag": dag(orders["id"], STRICT_CAST,
                                                                          dest={"id": "dst",
                                                                                "type": "destination.dataset",
                                                                                "config": {"name": "Never"}})})["id"]
    run_now(client, a, failing, django_capture_on_commit_callbacks)
    stats = client.get(url(a, "data-stats")).json()
    assert stats["datasets"] == 3 and stats["datasets_by_status"]["ready"] == 3  # orders, cities, KPI output
    assert stats["rows_total"] == 40 + 3 + 40
    assert stats["avg_quality"] is not None and 0 <= stats["avg_quality"] <= 100
    assert stats["pipelines_active"] == 2 and stats["pipelines_total"] == 2
    assert stats["runs_7d"] == {"succeeded": 1, "failed": 1, "total": 2} and stats["runs_active"] == 0
    assert stats["sources"] == 0 and stats["sources_active"] == 0 and stats["storage_bytes"] > 0
    empty = api_client_for(b.owner).get(url(b, "data-stats")).json()
    assert empty["datasets"] == 0 and empty["rows_total"] == 0 and empty["avg_quality"] is None
    assert empty["runs_7d"] == {"succeeded": 0, "failed": 0, "total": 0} and empty["pipelines_active"] == 0


# --- thread-runner dispatcher paths ------------------------------------------------------------------------------
def test_dispatcher_starts_a_retry_whose_backoff_elapsed(env, django_capture_on_commit_callbacks):
    from testing.db import db_tenant

    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Retry later", "dag": dag(orders["id"])})["id"]
    run_id = uuid.UUID(client.post(url(a, f"pipelines/{pid}/runs")).json()["run"]["id"])  # queued, never executed
    with a.tenant():
        PipelineRun.objects.filter(id=run_id).update(next_attempt_at=timezone.now() + dt.timedelta(minutes=5))
    assert scheduling.start_due_retry(a.org.id, run_id) == "retry_not_due"
    with a.tenant():
        PipelineRun.objects.filter(id=run_id).update(next_attempt_at=timezone.now() - dt.timedelta(seconds=5))
    with db_tenant(a.org.id):
        assert (a.org.id, run_id) in scheduling.scan_retries(timezone.now(), using="default")
    with django_capture_on_commit_callbacks(execute=True):
        assert scheduling.start_due_retry(a.org.id, run_id) == "retry_started"
    with a.tenant():
        assert PipelineRun.objects.get(id=run_id).status == "succeeded"
    assert scheduling.start_due_retry(a.org.id, run_id) == "retry_not_due"


def test_dispatcher_recovers_a_queued_run_lost_with_the_server(env, settings):
    from testing.db import db_tenant

    a, _b, client, orders, _cities = env
    settings.PIPELINE_HEARTBEAT_TIMEOUT_S = 60
    pid = create_pipeline(client, a, {"name": "Lost", "dag": dag(orders["id"])})["id"]
    run_id = uuid.UUID(client.post(url(a, f"pipelines/{pid}/runs")).json()["run"]["id"])
    with db_tenant(a.org.id):
        assert (a.org.id, run_id) not in scheduling.scan_retries(timezone.now(), using="default")  # fresh: not lost
    with a.tenant():
        PipelineRun.objects.filter(id=run_id).update(updated_at=timezone.now() - dt.timedelta(minutes=5))
    with db_tenant(a.org.id):
        assert (a.org.id, run_id) in scheduling.scan_retries(timezone.now(), using="default")
    assert scheduling.start_due_retry(a.org.id, run_id) == "retry_started"
    with a.tenant():
        lease = PipelineRun.objects.get(id=run_id).next_attempt_at
    assert lease > timezone.now()  # leased: not re-dispatched until the lease expires
    assert scheduling.start_due_retry(a.org.id, run_id) == "retry_not_due"


def test_ingest_finished_signal_finalizes_a_waiting_run(env, django_capture_on_commit_callbacks):
    from apps.datasets.models import Dataset
    from apps.datasets.signals import dataset_ingest_finished
    from testing.db import db_tenant

    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Finalize", "dag": dag(orders["id"])})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    with a.tenant():
        run = PipelineRun.objects.get(id=accepted["run"]["id"])
        assert run.status == "succeeded" and run.ingest_job_id
        # simulate the thread runner: the run is still waiting for its output ingestion
        PipelineRun.objects.filter(id=run.id).update(status="running", finished_at=None)
    with db_tenant(a.org.id):
        assert (a.org.id, run.id) in scheduling.scan_finalizable(using="default")
    dataset_ingest_finished.send(sender=Dataset, org_id=a.org.id, dataset_id=run.output_dataset_id,
                                 job_id=run.ingest_job_id, ok=True, code=None)
    with a.tenant():
        assert PipelineRun.objects.get(id=run.id).status == "succeeded"
    assert runner.finalize_for_ingest_job(a.org.id, run.ingest_job_id) == []  # nothing left to finalize


def test_thread_mode_dispatch_pass_finalizes_and_reports_counts(env, django_capture_on_commit_callbacks, settings,
                                                               monkeypatch):
    a, _b, client, orders, _cities = env
    pid = create_pipeline(client, a, {"name": "Pass", "dag": dag(orders["id"])})["id"]
    accepted = run_now(client, a, pid, django_capture_on_commit_callbacks)
    with a.tenant():
        PipelineRun.objects.filter(id=accepted["run"]["id"]).update(status="running", finished_at=None)
    settings.TASK_RUNNER, settings.CELERY_TASK_ALWAYS_EAGER = "thread", False
    target = (a.org.id, uuid.UUID(accepted["run"]["id"]))
    monkeypatch.setattr(scheduling, "scan_finalizable", lambda using="ops": [target])
    monkeypatch.setattr(scheduling, "scan_retries", lambda now, using="ops": [])
    monkeypatch.setattr(scheduling, "scan_due", lambda now, using="ops": [])
    monkeypatch.setattr(scheduling, "scan_stuck", lambda now, using="ops": [])
    assert scheduling.dispatch_due(using="default") == {"finalize_succeeded": 1}
    with a.tenant():
        assert PipelineRun.objects.get(id=accepted["run"]["id"]).status == "succeeded"
