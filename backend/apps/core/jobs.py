"""Background jobs + event streams (Redis Streams) consumed by the SSE endpoint.

    job = create_job("ingest", user_id=request.user.pk)
    some_task.delay(org_id=str(job.org_id), job_id=str(job.id))
    ...inside the task:
    publish(job.id, "status", {"status": "running", "progress": 10, "message": "Parsing"})
    finish(job.id, result={...})          # or fail(job.id, code="parse_error", message="...")
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import redis
from django.conf import settings
from django.utils import timezone

from .tenancy.context import require_org_id

STREAM_TTL_S = 24 * 3600
STREAM_MAXLEN = 2000
_redis_client = None


def redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(settings.REDIS_URL)
    return _redis_client


def stream_key(org_id: UUID | str, job_id: UUID | str) -> str:
    return f"org:{org_id}:job:{job_id}"


def create_job(type: str, *, user_id=None, idempotency_key: str | None = None, message: str = ""):
    from .models import Job

    require_org_id()
    if idempotency_key:
        existing = Job.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing
    return Job.objects.create(type=type, user_id=user_id, idempotency_key=idempotency_key, message=message)


def publish(job_id: UUID | str, event: str, data: dict[str, Any]) -> str | None:
    """Append an event to the job's stream. Never raises (streaming is best-effort)."""
    org_id = require_org_id()
    key = stream_key(org_id, job_id)
    try:
        r = redis_client()
        eid = r.xadd(key, {"event": event, "data": json.dumps(data, default=str)}, maxlen=STREAM_MAXLEN, approximate=True)
        r.expire(key, STREAM_TTL_S)
        return eid.decode() if isinstance(eid, bytes) else eid
    except redis.RedisError:
        return None


def update(job_id, *, status: str | None = None, progress: float | None = None, message: str | None = None) -> None:
    from .models import Job

    fields: dict[str, Any] = {}
    if status:
        fields["status"] = status
    if progress is not None:
        fields["progress"] = progress
    if message is not None:
        fields["message"] = message[:300]
    if fields:
        Job.objects.filter(id=job_id).update(**fields)
    publish(job_id, "status", {"status": status, "progress": progress, "message": message})


def finish(job_id, *, result: dict | None = None) -> None:
    from .models import Job

    Job.objects.filter(id=job_id).update(status="succeeded", progress=100, result=result, finished_at=timezone.now())
    publish(job_id, "result", {"result": result})
    publish(job_id, "done", {"status": "succeeded"})


def fail(job_id, *, code: str, message: str, detail: dict | None = None) -> None:
    from .models import Job

    err = {"code": code, "message": message, **({"detail": detail} if detail else {})}
    Job.objects.filter(id=job_id).update(status="failed", error=err, finished_at=timezone.now())
    publish(job_id, "error", err)
    publish(job_id, "done", {"status": "failed"})
