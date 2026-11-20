"""W3 pipelines: local runtime (runner modes, dispatcher paths), per-node errors, preview node_id, payload aliases.

Pure tests (no database): DB-touching collaborators are replaced by fakes via monkeypatch.
"""

from __future__ import annotations

import datetime as dt
import sys
import types
import uuid

import polars as pl
import pytest
from django.utils import timezone

from apps.pipelines import engine, runner, scheduling, services
from apps.pipelines import tasks as pipeline_tasks
from apps.pipelines.dag import DagIssue, validate_dag
from apps.pipelines.serializers import PreviewSerializer, ScheduleSerializer


# ------------------------------------------------------------------------------------------------ runner modes
@pytest.mark.parametrize(("runner_setting", "eager", "expected"), [
    ("celery", False, "celery"), ("thread", False, "thread"), ("eager", False, "eager"), ("thread", True, "eager"),
    ("bogus", False, "thread"),
])
def test_runner_mode(settings, runner_setting, eager, expected):
    settings.TASK_RUNNER = runner_setting
    settings.CELERY_TASK_ALWAYS_EAGER = eager
    assert runner.runner_mode() == expected


class _Task:
    def __init__(self):
        self.calls = []

    def apply_async(self, **kw):
        self.calls.append(kw)


def test_enqueue_later_defers_to_the_dispatcher_on_the_thread_runner(settings, monkeypatch):
    monkeypatch.setattr(runner.transaction, "on_commit", lambda fn: fn())
    task = _Task()
    settings.TASK_RUNNER, settings.CELERY_TASK_ALWAYS_EAGER = "thread", False
    assert runner.enqueue_later(task, {"run_id": "r"}, 30) is False and task.calls == []
    settings.TASK_RUNNER = "celery"
    assert runner.enqueue_later(task, {"run_id": "r"}, 30) is True
    assert task.calls == [{"kwargs": {"run_id": "r"}, "countdown": 30}]


# ------------------------------------------------------------------------------------------------ dispatcher paths
def test_celery_beat_task_is_a_noop_unless_the_runner_is_celery(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduling, "dispatch_due", lambda *a, **k: calls.append(1) or {"enqueued": 1})
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.TASK_RUNNER = "thread"
    assert pipeline_tasks.dispatch_due.run() is None and calls == []
    settings.TASK_RUNNER = "celery"
    assert pipeline_tasks.dispatch_due.run() == {"enqueued": 1} and calls == [1]


def test_local_tick_is_a_noop_under_celery(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduling, "dispatch_due", lambda *a, **k: calls.append(1) or {"busy": 1})
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.TASK_RUNNER = "celery"
    assert scheduling.local_tick() is None and calls == []
    settings.TASK_RUNNER = "thread"
    assert scheduling.local_tick() == {"busy": 1} and calls == [1]


def test_only_one_path_dispatches_for_every_runner(settings, monkeypatch):
    """For each TASK_RUNNER exactly one of (celery beat task, local scheduler tick) reaches dispatch_due."""
    import django.db

    monkeypatch.setattr(django.db, "close_old_connections", lambda: None)
    settings.CELERY_TASK_ALWAYS_EAGER = False
    for mode in ("celery", "thread", "eager"):
        settings.TASK_RUNNER = mode
        calls = []
        monkeypatch.setattr(scheduling, "dispatch_due", lambda *a, _c=calls, **k: _c.append(1) or {})
        pipeline_tasks.dispatch_due.run()
        scheduling.local_tick()
        assert len(calls) == 1, mode


def test_register_local_dispatcher_uses_the_core_scheduler(monkeypatch):
    registered = []
    fake = types.ModuleType("apps.core.scheduler")
    fake.register_periodic = lambda name, every, fn: registered.append((name, every, fn))
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", fake)
    assert scheduling.register_local_dispatcher() is True
    assert registered == [("pipelines.dispatch", 60, scheduling.local_tick)]


def test_register_local_dispatcher_skips_when_the_scheduler_is_missing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", None)  # import raises ImportError
    assert scheduling.register_local_dispatcher() is False
    assert "not available" in caplog.text


def test_dispatch_due_backs_off_when_another_process_holds_the_lock(monkeypatch):
    monkeypatch.setattr(scheduling, "_try_lock", lambda using: False)
    monkeypatch.setattr(scheduling, "_unlock", lambda using: pytest.fail("must not unlock a lock it does not hold"))
    assert scheduling.dispatch_due(timezone.now()) == {"busy": 1}


def _fake_scans(monkeypatch, *, due=(), stuck=(), retries=(), finalizable=()):
    monkeypatch.setattr(scheduling, "scan_due", lambda now, using="ops": list(due))
    monkeypatch.setattr(scheduling, "scan_stuck", lambda now, using="ops": list(stuck))
    monkeypatch.setattr(scheduling, "scan_retries", lambda now, using="ops": list(retries))
    monkeypatch.setattr(scheduling, "scan_finalizable", lambda using="ops": list(finalizable))


def test_dispatch_due_thread_mode_starts_due_retries_and_finalizes(settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER, settings.TASK_RUNNER = False, "thread"
    org, run1, run2, sched = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _fake_scans(monkeypatch, due=[(org, sched)], retries=[(org, run1)], finalizable=[(org, run2)])
    seen = []
    monkeypatch.setattr(scheduling, "fire_schedule", lambda o, s, now: seen.append(("fire", s)) or "enqueued")
    monkeypatch.setattr(scheduling, "start_due_retry", lambda o, r, now: seen.append(("retry", r)) or "retry_started")
    monkeypatch.setattr(scheduling, "_finalize", lambda o, r: seen.append(("finalize", r)) or "finalize_waiting")
    locks = []
    monkeypatch.setattr(scheduling, "_try_lock", lambda using: locks.append("lock") or True)
    monkeypatch.setattr(scheduling, "_unlock", lambda using: locks.append("unlock"))
    counts = scheduling.dispatch_due(timezone.now())
    assert counts == {"finalize_waiting": 1, "retry_started": 1, "enqueued": 1}
    assert seen == [("finalize", run2), ("retry", run1), ("fire", sched)]
    assert locks == ["lock", "unlock"]


def test_dispatch_due_celery_mode_leaves_retries_to_countdown_tasks(settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER, settings.TASK_RUNNER = False, "celery"
    _fake_scans(monkeypatch, retries=[(uuid.uuid4(), uuid.uuid4())], finalizable=[(uuid.uuid4(), uuid.uuid4())])
    monkeypatch.setattr(scheduling, "start_due_retry", lambda *a: pytest.fail("celery uses countdown retries"))
    monkeypatch.setattr(scheduling, "_finalize", lambda *a: pytest.fail("celery uses finalize polling"))
    assert scheduling.dispatch_due(timezone.now(), locked=False) == {}


def test_one_broken_org_does_not_stop_the_dispatcher(settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER, settings.TASK_RUNNER = False, "thread"
    good, bad = uuid.uuid4(), uuid.uuid4()
    _fake_scans(monkeypatch, retries=[(bad, bad), (good, good)])

    def start(org, run, now):
        if org == bad:
            raise RuntimeError("boom")
        return "retry_started"

    monkeypatch.setattr(scheduling, "start_due_retry", start)
    assert scheduling.dispatch_due(timezone.now(), locked=False) == {"retry_error": 1, "retry_started": 1}


def test_retry_scan_sql_also_recovers_lost_queued_runs():
    sql = scheduling.RETRY_DUE_SQL
    assert "status = 'queued'" in sql and "next_attempt_at <= %s" in sql
    assert "next_attempt_at IS NULL AND updated_at < %s" in sql
    assert "status = 'running' AND ingest_job_id IS NOT NULL" in scheduling.FINALIZE_SQL


# ------------------------------------------------------------------------------------------------ finalize timing
class _Run:
    def __init__(self, steps, started_at=None):
        self.steps = steps
        self.started_at = started_at


def test_handoff_is_the_destination_step_start():
    t = timezone.now() - dt.timedelta(minutes=3)
    run = _Run([{"type": "source.dataset", "started_at": (t - dt.timedelta(hours=2)).isoformat()},
                {"type": "destination.dataset", "started_at": t.isoformat()}], started_at=t - dt.timedelta(hours=2))
    assert runner.handoff_at(run) == t
    assert runner._waited_too_long(run) is False
    old = _Run([{"type": "destination.dataset", "started_at": (timezone.now() - dt.timedelta(hours=2)).isoformat()}])
    assert runner._waited_too_long(old) is True
    assert runner.handoff_at(_Run([], started_at=t)) == t


# ------------------------------------------------------------------------------------------------ per-node errors
def test_issue_dict_names_the_node_and_a_pinnable_field_path():
    assert DagIssue("pick", "config.columns", "unknown_column", "Unknown column.").as_dict() == {
        "node_id": "pick", "node": "pick", "field": "nodes.pick.config.columns", "code": "unknown_column",
        "message": "Unknown column."}
    assert DagIssue("pick", "", "x", "m").as_dict()["field"] == "nodes.pick"
    assert DagIssue(None, "edges", "cycle", "m").as_dict() == {
        "node_id": None, "node": None, "field": "edges", "code": "cycle", "message": "m"}


def test_validation_payload_errors_carry_node_ids():
    bad = {"nodes": [{"id": "src", "type": "source.dataset", "config": {"dataset_id": str(uuid.uuid4())}},
                     {"id": "evil", "type": "python", "config": {}},
                     {"id": "dst", "type": "destination.dataset", "config": {"name": "x"}}],
           "edges": [{"from": "src", "to": "evil"}, {"from": "evil", "to": "dst"}]}
    payload = validate_dag(bad).payload()
    assert payload["valid"] is False
    evil = [e for e in payload["errors"] if e["node_id"] == "evil"]
    assert evil and all(e["field"].startswith("nodes.evil") for e in evil)


# ------------------------------------------------------------------------------------------------ preview node_id
SOURCE = pl.DataFrame({"city": [" Pune", "Delhi ", " Pune", None], "amount": ["10", "20", "30", "40"]})


def _dag():
    nodes = [
        {"id": "src", "type": "source.dataset", "config": {"dataset_id": str(uuid.uuid4())}},
        {"id": "trim", "type": "trim_whitespace", "config": {"columns": ["city"]}},
        {"id": "drop", "type": "drop_nulls", "config": {"columns": ["city"]}},
        {"id": "dst", "type": "destination.dataset", "config": {"name": "Out"}},
    ]
    return {"nodes": nodes, "edges": [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]}


def test_engine_stop_after_returns_that_nodes_output():
    validation = validate_dag(_dag())
    assert validation.ok, validation.payload()
    io = engine.RunIO(load_source=lambda node, limit: SOURCE.head(limit) if limit else SOURCE)
    full = engine.execute(validation, io)
    assert full.frame.height == 3 and full.frame["city"].to_list() == ["Pune", "Delhi", "Pune"]
    partial = engine.execute(validation, io, stop_after="trim")
    assert partial.frame.height == 4  # drop_nulls not applied
    status = {m.id: m.status for m in partial.steps}
    assert status == {"src": "succeeded", "trim": "succeeded", "drop": "skipped", "dst": "skipped"}
    only_source = engine.execute(validation, io, stop_after="src")
    assert only_source.frame["city"].to_list() == SOURCE["city"].to_list()


@pytest.fixture
def pure_services(monkeypatch):
    """services.validate without DB resolvers and a fake source loader."""
    monkeypatch.setattr(services, "validate", lambda dag, user_id=None: validate_dag(dag))
    monkeypatch.setattr(services, "_preview_load_source", lambda user_id: (
        lambda node, limit: SOURCE.head(limit) if limit else SOURCE))
    monkeypatch.setattr(services, "_preview_load_dataset", lambda user_id: (lambda ds, limit: SOURCE))


def test_preview_node_id_and_limit(pure_services):
    out = services.preview(_dag(), limit=2, node_id="trim")
    assert out["ok"] is True and out["node_id"] == "trim"
    assert out["row_count"] == 2 and out["rows"] == [["Pune", "10"], ["Delhi", "20"]]
    assert [c["name"] for c in out["columns"]] == ["city", "amount"]
    assert {s["step_key"]: s["status"] for s in out["steps"]} == {"src": "succeeded", "trim": "succeeded",
                                                                  "drop": "skipped"}
    assert services.preview(_dag(), limit=50)["row_count"] == 3


def test_preview_unknown_node_is_a_400(pure_services):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        services.preview(_dag(), node_id="nope")
    assert "node_id" in exc.value.detail


def test_dry_run_valid_dag_returns_validation_and_sample_rows(pure_services):
    out = services.dry_run(_dag(), limit=50, node_id="drop")
    assert out["valid"] is True and out["ok"] is True and out["errors"] == []
    assert out["row_count"] == 3 and out["node_id"] == "drop" and "schemas" in out


def test_dry_run_invalid_dag_returns_per_node_errors_without_rows(pure_services):
    bad = _dag()
    bad["nodes"][2]["config"] = {"columns": "not-a-list"}
    out = services.dry_run(bad)
    assert out["valid"] is False and out["ok"] is False and out["rows"] == []
    assert any(e["node_id"] == "drop" and e["field"].startswith("nodes.drop") for e in out["errors"])


def test_dry_run_runtime_failure_is_pinned_on_the_failing_node(pure_services, monkeypatch):
    def boom(validation, io, **kw):
        raise engine.RunFailure("cast_failed", "Could not cast amount.", node="trim")

    monkeypatch.setattr(services.engine, "execute", boom)
    out = services.dry_run(_dag())
    assert out["valid"] is True and out["ok"] is False
    assert out["errors"] == [{"node_id": "trim", "node": "trim", "field": "nodes.trim.data", "code": "cast_failed",
                              "message": "Could not cast amount."}]


def test_connector_preview_runs_in_process_without_celery(settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER, settings.TASK_RUNNER = False, "thread"
    from apps.connectors import services as connectors

    seen = {}

    def read_frame(source_id, table, **kw):
        seen.update(kw, source_id=source_id, table=table)
        return pl.DataFrame({"a": ["1"]})

    monkeypatch.setattr(connectors, "read_frame", read_frame)
    frame = services._read_connector_preview({"org_id": "o", "data_source_id": "s", "table": "t", "schema": "public",
                                              "columns": None, "limit": 5})
    assert frame.height == 1 and seen["table"] == "t" and seen["limit"] == 5 and seen["org_id"] == "o"


# ------------------------------------------------------------------------------------------------ builder joins
ORDERS_DS, CITIES_DS = str(uuid.uuid4()), str(uuid.uuid4())
CITIES = pl.DataFrame({"city": ["Pune", "Delhi"], "state": ["MH", "DL"]})


def _builder_join(**overrides):
    """The pipeline builder's join: the right dataset is a second source wired to the join's right handle."""
    nodes = [
        {"id": "src", "type": "source.dataset", "config": {"dataset_id": ORDERS_DS}, "position": {"x": 0, "y": 0}},
        {"id": "trim", "type": "trim_whitespace", "config": {"columns": ["city"]}},
        {"id": "cities", "type": "source.dataset", "config": {"dataset_id": CITIES_DS, "columns": ["city", "state"]}},
        {"id": "join", "type": "join", "config": {"how": "left", "on": [{"left": "city", "right": "city"}]}},
        {"id": "dst", "type": "destination.dataset", "config": {"name": "Joined"}},
    ]
    edges = [{"from": "src", "to": "trim"}, {"from": "trim", "to": "join", "port": "left"},
             {"from": "cities", "to": "join", "port": "right"}, {"from": "join", "to": "dst"}]
    dag = {"nodes": nodes, "edges": edges, "settings": {"layout": "auto"}}
    dag.update(overrides)
    return dag


def test_builder_join_right_input_is_folded_into_the_join_config():
    validation = validate_dag(_builder_join())
    assert validation.ok, validation.payload()
    assert [n.id for n in validation.order] == ["src", "trim", "join", "dst"]
    assert validation.folded == {"cities": "join"}
    join = next(n for n in validation.order if n.id == "join")
    assert str(join.config.dataset_id) == CITIES_DS and join.config.columns == ["city", "state"]
    io = engine.RunIO(load_source=lambda node, limit: SOURCE, load_dataset=lambda ds, limit: CITIES)
    out = engine.execute(validation, io)
    assert out.frame.columns == ["city", "amount", "state"]
    assert out.frame.filter(pl.col("city") == "Pune")["state"].to_list() == ["MH", "MH"]


def test_builder_join_without_a_left_handle_or_with_a_derived_right_side_is_pinned_on_the_join():
    no_left = _builder_join()
    no_left["edges"] = [{k: v for k, v in e.items() if k != "port"} for e in no_left["edges"]]
    payload = validate_dag(no_left).payload()
    codes = {(e["node_id"], e["code"]) for e in payload["errors"]}
    assert ("join", "join_inputs") in codes and ("join", "fan_in") not in codes

    derived = _builder_join()
    derived["nodes"].insert(3, {"id": "pick", "type": "select_columns", "config": {"columns": ["city"]}})
    derived["edges"] = [e for e in derived["edges"] if e["from"] != "cities"] + [
        {"from": "cities", "to": "pick"}, {"from": "pick", "to": "join", "port": "right"}]
    codes = {(e["node_id"], e["code"]) for e in validate_dag(derived).payload()["errors"]}
    assert ("join", "join_right_input") in codes and ("join", "fan_in") not in codes


def test_builder_join_conflicting_dataset_ids_are_refused():
    dag = _builder_join()
    dag["nodes"][3]["config"]["dataset_id"] = str(uuid.uuid4())
    errors = validate_dag(dag).payload()["errors"]
    assert {"node_id": "join", "node": "join", "field": "nodes.join.config.dataset_id",
            "code": "join_right_conflict",
            "message": "The join has both a right input and a different config.dataset_id."} in errors


def test_linear_dags_are_unchanged_and_ports_or_settings_are_accepted():
    linear = _dag()
    linear["edges"][0]["port"] = "left"
    linear["settings"] = {}
    validation = validate_dag(linear)
    assert validation.ok and validation.folded == {} and [n.id for n in validation.order] == [
        "src", "trim", "drop", "dst"]
    bad_port = _dag()
    bad_port["edges"][0]["port"] = "middle"
    assert not validate_dag(bad_port).ok


def test_preview_of_a_folded_source_points_to_the_join(pure_services):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        services.preview(_builder_join(), node_id="cities")
    assert "preview the join instead" in str(exc.value.detail["node_id"][0])


# ------------------------------------------------------------------------------------------------ payload aliases
def test_step_payload_adds_ui_names():
    step = {"id": "trim", "type": "trim_whitespace", "status": "succeeded"}
    assert services.step_payload(step) == {**step, "step_key": "trim", "step_type": "trim_whitespace"}


@pytest.mark.parametrize(("body", "enabled"), [
    ({"cron": "0 2 * * *"}, True),
    ({"cron": "0 2 * * *", "is_active": False}, False),
    ({"cron": "0 2 * * *", "enabled": False}, False),
    ({"cron": "0 2 * * *", "enabled": True, "is_active": False}, True),
])
def test_schedule_serializer_accepts_is_active(body, enabled):
    ser = ScheduleSerializer(data=body)
    assert ser.is_valid(), ser.errors
    assert ser.validated_data["enabled"] is enabled and "is_active" not in ser.validated_data


def test_preview_serializer_accepts_node_id_and_limit():
    ser = PreviewSerializer(data={"node_id": "trim", "limit": 50, "dag": {"nodes": [], "edges": []}})
    assert ser.is_valid(), ser.errors
    assert ser.validated_data["node_id"] == "trim" and ser.validated_data["limit"] == 50
    assert not PreviewSerializer(data={"limit": 500}).is_valid()
