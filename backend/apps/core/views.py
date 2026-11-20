"""Health endpoints (no auth).

    GET /healthz   liveness: always 200 while the process serves requests; reports the runtime and checks
    GET /readyz    readiness: 503 when a REQUIRED dependency is broken (database, cache table, storage, or a
                   CONFIGURED Redis); optional services that are not configured are reported, never errors

Check values: "ok" | "not_configured" (optional service absent, e.g. Redis without REDIS_URL) | "fs" / "s3" for a
healthy storage backend | "error: <reason>".
"""

from __future__ import annotations

import os

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

CACHE_TABLE_HINT = "run manage.py createcachetable"


def _check_database() -> str:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def _check_cache() -> str:
    cfg = (getattr(settings, "CACHES", {}) or {}).get("default", {})
    backend = str(cfg.get("BACKEND", ""))
    if backend.endswith("DatabaseCache"):
        table = cfg.get("LOCATION") or "nexora_cache"
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", [table])
                present = bool(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            return f"error: {type(exc).__name__}"
        if not present:
            return f"error: cache table '{table}' is missing ({CACHE_TABLE_HINT})"
    try:
        from django.core.cache import cache

        cache.get("nexora:readyz:probe")
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def _check_redis() -> str:
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return "not_configured"
    try:
        import redis

        redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2).ping()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def _check_storage() -> str:
    from . import storage

    name = storage.backend()
    try:
        if name == "fs":
            root = storage.storage_root()
            root.mkdir(parents=True, exist_ok=True)
            if not os.access(root, os.R_OK | os.W_OK):
                return "error: storage directory is not writable"
            return "fs"
        storage.client().head_bucket(Bucket=settings.S3_BUCKET)
        return "s3"
    except Exception as exc:  # noqa: BLE001
        return f"error: {type(exc).__name__}"


def runtime_info() -> dict:
    from . import jobs, runner, storage

    return {
        "task_runner": getattr(settings, "TASK_RUNNER", "celery"),
        "job_events": jobs.events_backend(),
        "storage": storage.backend(),
        "local_scheduler": bool(getattr(settings, "LOCAL_SCHEDULER", False)),
        "redis": "configured" if getattr(settings, "REDIS_URL", "") else "not_configured",
        "pool_threads": int(getattr(settings, "TASK_RUNNER_THREADS", 0) or 0) if runner.runner_name() == "thread" else 0,
    }


def collect_checks() -> dict[str, str]:
    return {
        "database": _check_database(),
        "cache": _check_cache(),
        "redis": _check_redis(),
        "storage": _check_storage(),
    }


def _healthy(value: str) -> bool:
    return not value.startswith("error")


def healthz(request):
    checks = collect_checks()
    return JsonResponse({"status": "ok", "checks": checks, "runtime": runtime_info()})


def readyz(request):
    checks = collect_checks()
    ok = all(_healthy(v) for v in checks.values())
    body = {"status": "ok" if ok else "degraded", "checks": checks, "runtime": runtime_info()}
    return JsonResponse(body, status=200 if ok else 503)
