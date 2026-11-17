from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"status": "ok"})


def readyz(request):
    checks: dict[str, str] = {}
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        import redis

        redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"
    try:
        from .storage import client

        client().head_bucket(Bucket=settings.S3_BUCKET)
        checks["object_storage"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["object_storage"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JsonResponse({"status": "ok" if ok else "degraded", "checks": checks}, status=200 if ok else 503)
