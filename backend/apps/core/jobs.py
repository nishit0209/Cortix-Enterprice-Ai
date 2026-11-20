"""Background jobs + event streams consumed by the SSE endpoint (`/orgs/<org>/jobs/<id>/events`).

    job = create_job("ingest", user_id=request.user.pk)
    some_task.delay(org_id=str(job.org_id), job_id=str(job.id))
    ...inside the task:
    publish(job.id, "status", {"status": "running", "progress": 10, "message": "Parsing"})
    finish(job.id, result={...})          # or fail(job.id, code="parse_error", message="...")

Event transport (`settings.JOB_EVENTS_BACKEND`, read at call time):
    "redis"  Redis Streams `org:<org>:job:<job>` (TTL 24 h)
    "db"     append-only `core.JobEvent` rows written on the durable alias (visible to the SSE poller at once, even
             while the publishing task's own transaction is still open). Purged after JOB_EVENT_RETENTION_DAYS.
"""

from __future__ import annotations

import json
import logging
import zlib
from typing import Any
from uuid import UUID

import redis
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from .tenancy.context import require_org_id, set_db_tenant

log = logging.getLogger(__name__)

STREAM_TTL_S = 24 * 3600
STREAM_MAXLEN = 2000
JOB_EVENT_RETENTION_DAYS = 7
MAX_EVENT_BYTES = 64 * 1024
_redis_client = None


def redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(settings.REDIS_URL)
    return _redis_client


def events_backend() -> str:
    backend = str(getattr(settings, "JOB_EVENTS_BACKEND", "") or "").lower()
    if backend in ("db", "redis"):
        return backend
    return "redis" if getattr(settings, "REDIS_URL", "") else "db"


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


def _encode(data: dict[str, Any]) -> str:
    raw = json.dumps(data, default=str)
    if len(raw.encode()) > MAX_EVENT_BYTES:  # an event is a progress line, never a payload dump
        raw = json.dumps({"truncated": True, "keys": sorted(map(str, data))[:50]})
    return raw


def publish(job_id: UUID | str, event: str, data: dict[str, Any]) -> str | None:
    """Append an event to the job's stream and return its event id. Never raises (streaming is best-effort);
    a missing tenant context still raises TenantContextMissing (programming error, fail closed)."""
    org_id = require_org_id()
    if events_backend() == "db":
        return _publish_db(org_id, job_id, event, data)
    key = stream_key(org_id, job_id)
    try:
        r = redis_client()
        eid = r.xadd(key, {"event": event, "data": _encode(data)}, maxlen=STREAM_MAXLEN, approximate=True)
        r.expire(key, STREAM_TTL_S)
        return eid.decode() if isinstance(eid, bytes) else eid
    except redis.RedisError:
        return None


def _lock_key(job_id: UUID | str) -> int:
    """Stable signed 32-bit key for pg_advisory_xact_lock (serialises seq allocation per job)."""
    value = zlib.crc32(str(job_id).encode())
    return value - (1 << 32) if value >= (1 << 31) else value


def _publish_db(org_id: UUID, job_id: UUID | str, event: str, data: dict[str, Any]) -> str | None:
    from django.db import connections
    from django.db.models import Max

    from .db import durable_alias
    from .models import JobEvent

    alias = durable_alias()
    payload = json.loads(_encode(data))
    try:
        with transaction.atomic(using=alias):
            set_db_tenant(org_id, using=alias)
            with connections[alias].cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", [0x4E58, _lock_key(job_id)])
            last = JobEvent.objects.using(alias).filter(job_id=job_id).aggregate(m=Max("seq"))["m"] or 0
            row = JobEvent(org_id=org_id, job_id=job_id, seq=last + 1, event=str(event)[:32], data=payload)
            row.save(using=alias)
        return str(row.seq)
    except (DatabaseError, ValueError):
        log.warning("job event %s for job %s could not be stored", event, job_id, exc_info=True)
        return None


def events_after(job_id: UUID | str, after_seq: int = 0, limit: int = 100) -> list[tuple[int, str, dict]]:
    """[(seq, event, data)] of a job after `after_seq` (inside a tenant context; RLS applies)."""
    from .models import JobEvent

    rows = (JobEvent.objects.filter(job_id=job_id, seq__gt=int(after_seq)).order_by("seq")
            .values_list("seq", "event", "data")[: max(1, int(limit))])
    return [(int(s), e, d) for s, e, d in rows]


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
