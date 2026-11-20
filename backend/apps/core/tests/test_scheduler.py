"""Periodic-job registry and the local scheduler (no database): due-time computation, the advisory-lock guard
(acquired / not acquired, with fakes), failure isolation, server-process detection and the Celery beat bridge."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.core import scheduler
from apps.core.scheduler import PeriodicJob, Scheduler


class MemoryStore:
    def __init__(self):
        self.values: dict[str, float] = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def lock_returning(acquired: bool, log: list | None = None):
    @contextmanager
    def lock():
        if log is not None:
            log.append("enter")
        yield acquired
        if log is not None:
            log.append("exit")

    return lock


def make_scheduler(jobs, *, acquired=True, clock=None, store=None, log=None):
    return Scheduler(lock=lock_returning(acquired, log), clock=clock or Clock(), store=store or MemoryStore(),
                     jobs=lambda: list(jobs))


# ------------------------------------------------------------------------------------------------ registry
def test_register_validates_and_replaces():
    calls = []
    try:
        job = scheduler.register_periodic("unit.job", 60, lambda: calls.append(1))
        assert scheduler.get_job("unit.job") is job and job.every_seconds == 60
        scheduler.register_periodic("unit.job", 120, lambda: None)  # same name replaces
        assert scheduler.get_job("unit.job").every_seconds == 120
        assert [j.name for j in scheduler.registered()].count("unit.job") == 1
        with pytest.raises(ValueError):
            scheduler.register_periodic("", 60, lambda: None)
        with pytest.raises(ValueError):
            scheduler.register_periodic("unit.too_fast", 1, lambda: None)
        with pytest.raises(TypeError):
            scheduler.register_periodic("unit.not_callable", 60, "nope")
    finally:
        scheduler.unregister_periodic("unit.job")
    assert scheduler.get_job("unit.job") is None


def test_core_registers_the_job_event_purge():
    job = scheduler.get_job("core.purge_job_events")
    assert job is not None and job.every_seconds == 6 * 3600
    from apps.core.ops import purge_job_events

    assert job.fn is purge_job_events


# ------------------------------------------------------------------------------------------------ due times
def test_due_time_computation():
    job = PeriodicJob("j", 60, lambda: None)
    assert scheduler.next_due_at(job, None, 500.0) == 500.0  # never ran: due now
    assert scheduler.next_due_at(job, 1000.0, 1010.0) == 1060.0
    assert not scheduler.is_due(job, 1000.0, 1059.9)
    assert scheduler.is_due(job, 1000.0, 1060.0)
    assert scheduler.is_due(job, None, 0.0)
    other = PeriodicJob("k", 3600, lambda: None)
    assert [j.name for j in scheduler.due_jobs([job, other], {"j": 1000.0, "k": 900.0}, 1070.0)] == ["j"]


def test_tick_runs_due_jobs_once_per_interval():
    ran = []
    clock, store = Clock(10_000.0), MemoryStore()
    jobs = [PeriodicJob("a", 60, lambda: ran.append("a")), PeriodicJob("b", 300, lambda: ran.append("b"))]
    s = make_scheduler(jobs, clock=clock, store=store)
    assert s.tick() == ["a", "b"]  # never ran -> both due
    clock.now += 30
    assert s.tick() == []
    clock.now += 30
    assert s.tick() == ["a"]
    clock.now += 240
    assert s.tick() == ["a", "b"]
    assert ran == ["a", "b", "a", "a", "b"]
    assert store.values == {"a": clock.now, "b": clock.now}


def test_tick_without_the_lock_does_nothing():
    ran, log = [], []
    s = make_scheduler([PeriodicJob("a", 60, lambda: ran.append(1))], acquired=False, log=log)
    assert s.tick() == [] and ran == []
    assert log == ["enter", "exit"]


def test_last_runs_are_shared_through_the_store():
    """Another process (sharing the store) does not re-run a job that just ran elsewhere."""
    store, clock, ran = MemoryStore(), Clock(), []
    job = PeriodicJob("shared", 60, lambda: ran.append(1))
    assert make_scheduler([job], store=store, clock=clock).tick() == ["shared"]
    clock.now += 10
    assert make_scheduler([job], store=store, clock=clock).tick() == []
    assert ran == [1]


def test_a_failing_job_does_not_stop_the_others_and_is_not_hot_looped(caplog):
    ran, clock = [], Clock()

    def boom():
        raise RuntimeError("job exploded")

    s = make_scheduler([PeriodicJob("a_boom", 60, boom), PeriodicJob("b_ok", 60, lambda: ran.append(1))],
                       clock=clock)
    assert s.tick() == ["a_boom", "b_ok"] and ran == [1]
    assert "job exploded" in caplog.text
    clock.now += 1
    assert s.tick() == []  # the failed job waits for its next interval too


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.last = sql

    def fetchone(self):
        return (self.conn.acquire,)


class _FakeConnection:
    def __init__(self, acquire):
        self.acquire = acquire
        self.executed = []

    def cursor(self):
        return _FakeCursor(self)


@pytest.mark.parametrize("acquire", [True, False])
def test_advisory_lock_try_and_release(monkeypatch, acquire):
    conn = _FakeConnection(acquire)
    monkeypatch.setattr("django.db.connection", conn)
    with scheduler.advisory_lock() as got:
        assert got is acquire
    sqls = [s for s, _ in conn.executed]
    assert sqls[0] == "SELECT pg_try_advisory_lock(%s)"
    assert conn.executed[0][1] == [scheduler.ADVISORY_LOCK_KEY]
    assert ("SELECT pg_advisory_unlock(%s)" in sqls) is acquire  # released only when it was acquired


def test_advisory_lock_without_a_database_is_not_acquired(monkeypatch):
    class Broken:
        def cursor(self):
            raise RuntimeError("no database")

    monkeypatch.setattr("django.db.connection", Broken())
    with scheduler.advisory_lock() as got:
        assert got is False


def test_last_run_store_falls_back_to_memory(monkeypatch):
    class BrokenCache:
        def get(self, *_a, **_k):
            raise RuntimeError("no cache table")

        def set(self, *_a, **_k):
            raise RuntimeError("no cache table")

    monkeypatch.setattr("django.core.cache.cache", BrokenCache())
    store = scheduler.LastRunStore()
    assert store.get("x") is None
    store.set("x", 12.5)
    assert store.get("x") == 12.5


def test_last_run_store_uses_the_shared_cache():
    from django.core.cache import cache

    store = scheduler.LastRunStore()
    store.set("unit.cache", 99.0)
    assert cache.get(scheduler.CACHE_PREFIX + "unit.cache") == 99.0
    assert scheduler.LastRunStore().get("unit.cache") == 99.0  # another instance (process) sees it
    cache.delete(scheduler.CACHE_PREFIX + "unit.cache")


# ------------------------------------------------------------------------------------------------ processes
@pytest.mark.parametrize(("argv", "env", "expected"), [
    (["manage.py", "runserver"], {"RUN_MAIN": "true"}, True),
    (["manage.py", "runserver"], {}, False),  # autoreloader parent
    (["manage.py", "runserver", "--noreload"], {}, True),
    (["C:\\venv\\Scripts\\uvicorn.exe", "nexora.asgi:application"], {}, True),
    (["/venv/lib/python3.12/site-packages/uvicorn/__main__.py", "nexora.asgi:application"], {}, True),
    (["/usr/bin/daphne", "nexora.asgi:application"], {}, True),
    (["gunicorn", "nexora.wsgi"], {}, True),
    (["manage.py", "migrate"], {}, False),
    (["manage.py", "shell"], {}, False),
    (["manage.py", "seed_demo"], {}, False),
    (["/venv/bin/pytest"], {}, False),
    (["/venv/bin/celery", "-A", "nexora", "worker"], {}, False),
    (["manage.py", "runserver"], {"RUN_MAIN": "true", "NEXORA_SCHEDULER": "0"}, False),
    (["manage.py", "migrate"], {"NEXORA_SCHEDULER": "1"}, True),
    ([], {}, False),
])
def test_server_process_detection(argv, env, expected):
    assert scheduler.is_server_process(argv, env) is expected


def test_maybe_start_respects_the_setting(settings, monkeypatch):
    started = []
    monkeypatch.setattr(scheduler, "start", lambda **_k: started.append(1))
    monkeypatch.setattr(scheduler, "is_server_process", lambda: True)
    settings.LOCAL_SCHEDULER = False
    assert scheduler.maybe_start() is False
    settings.LOCAL_SCHEDULER = True
    assert scheduler.maybe_start() is True and started == [1]
    monkeypatch.setattr(scheduler, "is_server_process", lambda: False)
    assert scheduler.maybe_start() is False and started == [1]


def test_the_daemon_thread_ticks_and_stops(monkeypatch):
    import threading

    monkeypatch.setattr("django.db.close_old_connections", lambda: None)
    ticked = threading.Event()
    fake = SimpleNamespace(tick=lambda: ticked.set() or [])
    thread = scheduler.start(tick_s=0.01, first_delay_s=0, scheduler=fake)
    try:
        assert thread.daemon and thread.name == "nexora-scheduler"
        assert ticked.wait(5)
        assert scheduler.start(tick_s=0.01, first_delay_s=0, scheduler=fake) is thread  # once per process
    finally:
        scheduler.stop(timeout=5)
    assert not thread.is_alive()


# ------------------------------------------------------------------------------------------------ celery beat
def test_beat_bridge_installs_one_entry_per_job():
    try:
        scheduler.register_periodic("unit.beat", 90, lambda: None)
        entries = scheduler.beat_schedule()
        entry = entries["nexora:unit.beat"]
        assert entry == {"task": "apps.core.scheduler.run_periodic", "schedule": timedelta(seconds=90),
                         "args": ("unit.beat",)}
        installed = {}
        sender = SimpleNamespace(scheduler=SimpleNamespace(update_from_dict=installed.update))
        scheduler.install_beat_entries(sender=sender)
        assert "nexora:unit.beat" in installed and "nexora:core.purge_job_events" in installed
        scheduler.install_beat_entries(sender=None)  # tolerated
    finally:
        scheduler.unregister_periodic("unit.beat")


def test_run_periodic_task_runs_the_registered_function(settings):
    settings.TASK_RUNNER = "eager"
    ran = []
    try:
        scheduler.register_periodic("unit.run", 60, lambda: ran.append(1))
        assert scheduler.run_periodic.delay("unit.run").get() is True
        assert scheduler.run_periodic.delay("unit.unknown").get() is False
    finally:
        scheduler.unregister_periodic("unit.run")
    assert ran == [1]
