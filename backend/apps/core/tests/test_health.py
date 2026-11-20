"""Health endpoints (/healthz liveness, /readyz readiness).

Optional services that are not configured (Redis without REDIS_URL) are reported, never errors; storage reports
its backend ("fs" / "s3"); the database check is real; a missing DatabaseCache table fails readiness with the
"run manage.py createcachetable" hint. Unit tests use fake connections; the `django_db` tests use the real DB.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.db import OperationalError, ProgrammingError
from django.test import RequestFactory

from apps.core import views

DB_CACHE = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "nexora_cache"}}
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MISSING_TABLE = "error: cache table 'nexora_cache' is missing (run manage.py createcachetable)"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.last = sql
        if self.conn.fail is not None:
            raise self.conn.fail

    def fetchone(self):
        return (self.conn.table_present,) if "to_regclass" in self.last else (1,)


class _Conn:
    def __init__(self, *, table_present: bool = True, fail: Exception | None = None):
        self.table_present, self.fail, self.executed = table_present, fail, []

    def cursor(self):
        return _Cursor(self)


class _Cache:
    def __init__(self, fail: Exception | None = None):
        self.fail, self.calls = fail, []

    def get(self, key, default=None):
        self.calls.append(key)
        if self.fail is not None:
            raise self.fail
        return default


@pytest.fixture
def env(settings, monkeypatch, tmp_path):
    """A healthy local runtime: DB cache table present, no Redis, fs storage in a writable temp dir."""
    settings.REDIS_URL = ""
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    settings.TASK_RUNNER = "thread"
    settings.TASK_RUNNER_THREADS = 3
    settings.JOB_EVENTS_BACKEND = "db"
    settings.LOCAL_SCHEDULER = True
    monkeypatch.setattr(views, "settings", SimpleNamespace(**{
        name: getattr(settings, name) for name in (
            "REDIS_URL", "STORAGE_BACKEND", "LOCAL_STORAGE_DIR", "TASK_RUNNER", "TASK_RUNNER_THREADS",
            "LOCAL_SCHEDULER", "S3_BUCKET")
    }, CACHES=DB_CACHE))
    conn, cache = _Conn(), _Cache()
    monkeypatch.setattr(views, "connection", conn)
    monkeypatch.setattr("django.core.cache.cache", cache)
    return SimpleNamespace(conn=conn, cache=cache, settings=views.settings)


def _call(view):
    resp = view(RequestFactory().get("/"))
    return resp.status_code, json.loads(resp.content)


# ------------------------------------------------------------------------------------------------ cache table
def test_missing_cache_table_fails_readiness_with_the_createcachetable_hint(env):
    env.conn.table_present = False
    status, body = _call(views.readyz)
    assert status == 503 and body["status"] == "degraded"
    assert body["checks"]["cache"] == MISSING_TABLE
    assert "manage.py createcachetable" in body["checks"]["cache"]
    assert env.cache.calls == []  # the table is checked first: the cache itself is never queried
    to_regclass = [p for s, p in env.conn.executed if "to_regclass" in s]
    assert to_regclass == [["nexora_cache"]]
    status, body = _call(views.healthz)  # liveness stays 200 and shows the same check
    assert status == 200 and body["status"] == "ok" and body["checks"]["cache"] == MISSING_TABLE


def test_custom_cache_table_name_is_reported(env):
    env.settings.CACHES = {"default": {**DB_CACHE["default"], "LOCATION": "my_cache"}}
    env.conn.table_present = False
    assert views._check_cache() == "error: cache table 'my_cache' is missing (run manage.py createcachetable)"


def test_present_cache_table_is_probed(env):
    assert views._check_cache() == "ok"
    assert env.cache.calls == ["nexora:readyz:probe"]
    env.cache.fail = ProgrammingError("permission denied for table nexora_cache")
    assert views._check_cache() == "error: ProgrammingError"


def test_non_database_cache_skips_the_table_check(env):
    env.settings.CACHES = LOCMEM_CACHE
    assert views._check_cache() == "ok"
    assert not any("to_regclass" in s for s, _ in env.conn.executed)


def test_unreachable_database_fails_both_database_and_cache(env):
    env.conn.fail = OperationalError("connection refused")
    status, body = _call(views.readyz)
    assert status == 503
    assert body["checks"]["database"] == "error: OperationalError"
    assert body["checks"]["cache"] == "error: OperationalError"
    assert "connection refused" not in json.dumps(body)  # error details (hosts, users) are never exposed


# ------------------------------------------------------------------------------------------------ redis / storage
def test_local_runtime_without_redis_is_ready(env):
    status, body = _call(views.readyz)
    assert status == 200 and body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "cache": "ok", "redis": "not_configured", "storage": "fs"}
    assert body["runtime"] == {"task_runner": "thread", "job_events": "db", "storage": "fs", "local_scheduler": True,
                               "redis": "not_configured", "pool_threads": 3}
    assert (env.settings.LOCAL_STORAGE_DIR).is_dir()  # the storage root is created on first check


def test_configured_redis_is_pinged(env, monkeypatch):
    import redis

    pings = []

    class FakeRedis:
        def __init__(self, fail):
            self.fail = fail

        def ping(self):
            pings.append(1)
            if self.fail:
                raise redis.ConnectionError("Error 111 connecting to secret-host:6379")
            return True

    env.settings.REDIS_URL = "redis://secret-host:6379/0"
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: FakeRedis(fail=False)))
    assert views._check_redis() == "ok" and pings == [1]
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: FakeRedis(fail=True)))
    status, body = _call(views.readyz)
    assert status == 503 and body["checks"]["redis"] == "error: ConnectionError"
    assert "secret-host" not in json.dumps(body["checks"])


def test_unwritable_storage_fails_readiness(env, monkeypatch):
    monkeypatch.setattr(views.os, "access", lambda path, mode: False)
    status, body = _call(views.readyz)
    assert status == 503 and body["checks"]["storage"] == "error: storage directory is not writable"


def test_s3_storage_checks_the_bucket(env, monkeypatch, settings):
    from apps.core import storage

    heads = []
    settings.STORAGE_BACKEND = "s3"
    env.settings.S3_BUCKET = "nexora-test"
    monkeypatch.setattr(storage, "client", lambda: SimpleNamespace(head_bucket=lambda Bucket: heads.append(Bucket)))
    assert views._check_storage() == "s3" and heads == ["nexora-test"]

    def missing(Bucket):
        raise RuntimeError("NoSuchBucket")

    monkeypatch.setattr(storage, "client", lambda: SimpleNamespace(head_bucket=missing))
    assert views._check_storage() == "error: RuntimeError"


def test_runtime_info_reports_no_pool_outside_the_thread_runner(env, settings):
    settings.TASK_RUNNER = env.settings.TASK_RUNNER = "eager"
    info = views.runtime_info()
    assert info["task_runner"] == "eager" and info["pool_threads"] == 0


# ------------------------------------------------------------------------------------------------ real database
@pytest.mark.django_db
def test_readyz_against_the_real_database(client, settings, tmp_path):
    settings.REDIS_URL = ""
    settings.STORAGE_BACKEND = "fs"
    settings.LOCAL_STORAGE_DIR = tmp_path / "storage"
    resp = client.get("/readyz")
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["checks"] == {"database": "ok", "cache": "ok", "redis": "not_configured", "storage": "fs"}
    live = client.get("/healthz")
    assert live.status_code == 200 and live.json()["status"] == "ok"


@pytest.mark.django_db
def test_a_missing_cache_table_is_detected_by_the_real_database(monkeypatch):
    """to_regclass() on the real connection: an absent table is reported with the hint, never queried."""
    absent = {"default": {**DB_CACHE["default"], "LOCATION": "nexora_cache_absent_probe"}}
    monkeypatch.setattr(views, "settings", SimpleNamespace(CACHES=absent))
    assert views._check_cache() == (
        "error: cache table 'nexora_cache_absent_probe' is missing (run manage.py createcachetable)")
    present = {"default": {**DB_CACHE["default"], "LOCATION": "core_job"}}  # any existing table
    monkeypatch.setattr(views, "settings", SimpleNamespace(CACHES=present))
    cache = _Cache()
    monkeypatch.setattr("django.core.cache.cache", cache)
    assert views._check_cache() == "ok" and cache.calls == ["nexora:readyz:probe"]
