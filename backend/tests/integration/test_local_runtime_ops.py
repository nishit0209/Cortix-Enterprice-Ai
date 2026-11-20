"""Local runtime operations (CONTRACT "Local runtime: Periodic jobs" + "Health", Backend `apps.core`).

* Scheduler: `apps.core.scheduler.register_periodic(name, every_seconds, fn)`; with LOCAL_SCHEDULER one daemon
  thread ticks, and a Postgres advisory lock (`pg_try_advisory_lock`) guarantees ONE ticker across processes.
  Checked with two concurrent ticks on separate connections, and with the lock held by another session.
  The scheduler must never run inside tests / management commands.
* Health: without Redis, `/readyz` reports `redis: not_configured` (not an error) and `storage: fs`; with the
  DatabaseCache table missing it fails readiness with a clear "run manage.py createcachetable" message.

Skips cleanly until the Backend W3 local runtime (`apps.core.scheduler`) is merged.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading
import time
import uuid

import pytest
from django.test import Client

LOCAL_RUNTIME = importlib.util.find_spec("apps.core.scheduler") is not None
scheduler = importlib.import_module("apps.core.scheduler") if LOCAL_RUNTIME else None

pytestmark = pytest.mark.skipif(
    not LOCAL_RUNTIME, reason="local runtime not implemented yet (Backend W3: apps.core.scheduler)"
)


def _tick_api():
    sched_cls = getattr(scheduler, "Scheduler", None)
    if sched_cls is None or not hasattr(sched_cls, "tick"):
        pytest.skip("apps.core.scheduler exposes no Scheduler().tick() to drive a tick from a test")
    return sched_cls


# ------------------------------------------------------------------------------------------------ scheduler
def test_register_periodic_is_the_public_entry_point():
    assert callable(getattr(scheduler, "register_periodic", None))


def test_scheduler_thread_never_runs_under_pytest():
    names = [t.name for t in threading.enumerate() if t.is_alive()]
    assert not [n for n in names if "sched" in n.lower()], f"a scheduler thread runs inside tests: {names}"


@pytest.mark.django_db(transaction=True)
def test_only_one_concurrent_tick_runs_a_due_job():
    sched_cls = _tick_api()
    runs: list[str] = []
    lock = threading.Lock()
    name = f"qa.only_one_ticker.{uuid.uuid4().hex[:8]}"

    def slow():
        with lock:
            runs.append(threading.current_thread().name)
        time.sleep(1.0)  # hold the advisory lock while the other process tries to tick

    job = scheduler.register_periodic(name, 60, slow)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def ticker():
        from django.db import connection

        try:
            barrier.wait(5)
            sched_cls(jobs=lambda: [job]).tick()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    try:
        threads = [threading.Thread(target=ticker, name=f"qa-ticker-{i}") for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
    finally:
        unregister = getattr(scheduler, "unregister_periodic", None)
        if unregister:
            unregister(name)
    assert errors == []
    assert len(runs) == 1, f"the advisory lock must allow exactly one ticker, ran on {runs}"


@pytest.mark.django_db(transaction=True)
def test_no_tick_while_another_process_holds_the_lock():
    sched_cls = _tick_api()
    key = getattr(scheduler, "ADVISORY_LOCK_KEY", None)
    if key is None:
        pytest.skip("apps.core.scheduler.ADVISORY_LOCK_KEY not exposed")
    from testing.db import independent_connection

    ran: list[int] = []
    name = f"qa.lock_held.{uuid.uuid4().hex[:8]}"
    job = scheduler.register_periodic(name, 60, lambda: ran.append(1))
    try:
        with independent_connection(autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", [key])
            assert sched_cls(jobs=lambda: [job]).tick() in ([], None)
            assert ran == [], "a tick ran although another session holds the scheduler lock"
            other.execute("SELECT pg_advisory_unlock(%s)", [key])
        sched_cls(jobs=lambda: [job]).tick()
        assert ran == [1], "once the lock is free the due job runs"
    finally:
        unregister = getattr(scheduler, "unregister_periodic", None)
        if unregister:
            unregister(name)


# ------------------------------------------------------------------------------------------------ health
def _checks(resp) -> dict:
    body = resp.json()
    return {**body, **(body.get("checks") or {})}


@pytest.mark.django_db
def test_readyz_without_redis_reports_not_configured_and_fs_storage(settings, tmp_path):
    settings.REDIS_URL = ""
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    resp = Client().get("/readyz")
    checks = _checks(resp)
    assert checks.get("redis") == "not_configured", checks
    assert checks.get("storage") == "fs", checks
    assert resp.status_code == 200, f"Redis is optional: readiness must not fail without it ({checks})"


@pytest.mark.django_db
def test_readyz_reports_a_missing_cache_table_clearly(settings, tmp_path):
    settings.REDIS_URL = ""
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "qa_missing_cache_table",
        }
    }
    resp = Client().get("/readyz")
    assert resp.status_code == 503, resp.content[:300]
    assert "createcachetable" in resp.content.decode(), resp.content[:300]


@pytest.mark.django_db
def test_healthz_stays_alive_without_redis(settings):
    settings.REDIS_URL = ""
    resp = Client().get("/healthz")
    assert resp.status_code == 200
