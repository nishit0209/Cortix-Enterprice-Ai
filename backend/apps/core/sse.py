"""Server-Sent Events for job streams: GET /api/v1/orgs/<org>/jobs/<job_id>/events

No DB transaction is held while streaming: ownership is checked once, then Redis Streams are read.
Supports Last-Event-ID resume, 15s heartbeats and a 10-minute max lifetime.
"""

from __future__ import annotations

import asyncio
import time

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse

from .jobs import stream_key
from .tenancy.context import tenant_context
from .tenancy.views import resolve_membership

MAX_LIFETIME_S = 600
TERMINAL = {"done"}


def _check_access(user_id, org_slug: str, job_id) -> str | None:
    from .models import Job

    info = resolve_membership(user_id, org_slug)
    if info is None:
        return None
    with tenant_context(info.org_id, info=info):
        if not Job.objects.filter(id=job_id).exists():
            return None
    return str(info.org_id)


def sse_format(event_id: str, event: str, data: str) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {data}\n\n"


async def stream_events(key: str, last_id: str = "0-0", max_lifetime_s: int = MAX_LIFETIME_S):
    r = aioredis.from_url(settings.REDIS_URL)
    deadline = time.monotonic() + max_lifetime_s
    try:
        yield "retry: 3000\n\n"
        while time.monotonic() < deadline:
            resp = await r.xread({key: last_id}, block=15000, count=100)
            if not resp:
                yield ":hb\n\n"
                continue
            for _key, entries in resp:
                for eid, fields in entries:
                    eid_s = eid.decode() if isinstance(eid, bytes) else eid
                    last_id = eid_s
                    event = fields.get(b"event", b"message").decode()
                    data = fields.get(b"data", b"{}").decode()
                    yield sse_format(eid_s, event, data)
                    if event in TERMINAL:
                        return
    except asyncio.CancelledError:  # client disconnected
        raise
    finally:
        await r.aclose()


async def job_events(request, org: str, job_id):
    user = await request.auser()
    if not user.is_authenticated:
        return JsonResponse({"code": "not_authenticated", "status": 401}, status=401)
    org_id = await sync_to_async(_check_access)(user.pk, org, job_id)
    if org_id is None:
        return JsonResponse({"code": "not_found", "status": 404}, status=404)
    last_id = request.headers.get("Last-Event-ID") or "0-0"
    resp = StreamingHttpResponse(stream_events(stream_key(org_id, job_id), last_id), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
