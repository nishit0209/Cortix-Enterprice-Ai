"""Server-Sent Events for job streams: GET /api/v1/orgs/<org>/jobs/<job_id>/events

No DB transaction is held while streaming: access is checked once (org member; the job's own user unless the
member holds `jobs.view_all`, exactly like `GET /jobs/<id>`), then events are read from
  * Redis Streams (JOB_EVENTS_BACKEND="redis"; `Last-Event-ID` = the stream entry id), or
  * `core.JobEvent` rows (JOB_EVENTS_BACKEND="db"; `Last-Event-ID` = `seq`), polled with short queries, each in
    its own tenant transaction.
Both send `retry: 3000` first, a `:hb` keep-alive comment after 15 s without events, end after the terminal
`done` event, and live at most 10 minutes.

The body iterator matches the server: an ASGI request (uvicorn/daphne) gets an async generator; a WSGI request
(`manage.py runserver`, gunicorn sync workers, the Django test client) gets a plain generator. Django buffers a
mismatched iterator completely (`StreamingHttpResponse.__iter__` over an async iterator collects it with
`async_to_sync`), which would hold every frame - keep-alives included - until `done`.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID

import redis.asyncio as aioredis
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.handlers.asgi import ASGIRequest
from django.http import JsonResponse, StreamingHttpResponse

from . import jobs
from .jobs import events_after, events_backend, stream_key
from .tenancy.context import tenant_context
from .tenancy.views import resolve_membership

MAX_LIFETIME_S = 600
HEARTBEAT_S = 15
POLL_INTERVAL_S = 0.5
MAX_POLL_INTERVAL_S = 2.0
TERMINAL = {"done"}


def _check_access(user_id, org_slug: str, job_id) -> str | None:
    from .models import Job

    info = resolve_membership(user_id, org_slug)
    if info is None:
        return None
    with tenant_context(info.org_id, info=info):
        visible = Job.objects.filter(id=job_id)
        if not info.has_perm("jobs.view_all"):  # same visibility as JobViewSet.get_queryset
            visible = visible.filter(user_id=user_id)
        if not visible.exists():
            return None
    return str(info.org_id)


def sse_format(event_id: str, event: str, data: str) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {data}\n\n"


def _redis_frames(resp):
    """Yield (entry_id, frame, is_terminal) for each entry of an XREAD reply."""
    for _key, entries in resp:
        for eid, fields in entries:
            eid_s = eid.decode() if isinstance(eid, bytes) else eid
            event = fields.get(b"event", b"message").decode()
            data = fields.get(b"data", b"{}").decode()
            yield eid_s, sse_format(eid_s, event, data), event in TERMINAL


async def stream_events(key: str, last_id: str = "0-0", max_lifetime_s: int = MAX_LIFETIME_S):
    r = aioredis.from_url(settings.REDIS_URL)
    deadline = time.monotonic() + max_lifetime_s
    try:
        yield "retry: 3000\n\n"
        while time.monotonic() < deadline:
            resp = await r.xread({key: last_id}, block=HEARTBEAT_S * 1000, count=100)
            if not resp:
                yield ":hb\n\n"
                continue
            for eid, frame, terminal in _redis_frames(resp):
                last_id = eid
                yield frame
                if terminal:
                    return
    except asyncio.CancelledError:  # client disconnected
        raise
    finally:
        await r.aclose()


def stream_events_sync(key: str, last_id: str = "0-0", max_lifetime_s: int = MAX_LIFETIME_S, *, client=None):
    """WSGI twin of `stream_events` on the shared (thread-safe, pooled) sync Redis client."""
    r = client or jobs.redis_client()  # module lookup: patchable
    deadline = time.monotonic() + max_lifetime_s
    yield "retry: 3000\n\n"
    while time.monotonic() < deadline:
        resp = r.xread({key: last_id}, block=HEARTBEAT_S * 1000, count=100)
        if not resp:
            yield ":hb\n\n"
            continue
        for eid, frame, terminal in _redis_frames(resp):
            last_id = eid
            yield frame
            if terminal:
                return


def _fetch_db_events(org_id: str, job_id, after_seq: int) -> list[tuple[int, str, dict]]:
    """One short query in its own tenant transaction (no transaction spans the stream)."""
    with tenant_context(UUID(str(org_id))):
        return events_after(job_id, after_seq, limit=100)


async def stream_db_events(org_id: str, job_id, last_seq: int = 0, max_lifetime_s: int = MAX_LIFETIME_S, *,
                           fetch=None, sleep=asyncio.sleep, clock=time.monotonic,
                           heartbeat_s: float = HEARTBEAT_S, poll_s: float = POLL_INTERVAL_S):
    """Poll `core.JobEvent` rows after `last_seq`. The poll interval backs off to MAX_POLL_INTERVAL_S while idle."""
    fetch = fetch or sync_to_async(_fetch_db_events, thread_sensitive=True)
    deadline = clock() + max_lifetime_s
    last_beat = clock()
    interval = poll_s
    yield "retry: 3000\n\n"
    while clock() < deadline:
        rows = await fetch(org_id, job_id, last_seq)
        if rows:
            interval = poll_s
            last_beat = clock()
            for seq, event, data in rows:
                last_seq = seq
                yield sse_format(str(seq), event, json.dumps(data, default=str))
                if event in TERMINAL:
                    return
            continue
        if clock() - last_beat >= heartbeat_s:
            last_beat = clock()
            yield ":hb\n\n"
        await sleep(interval)
        interval = min(MAX_POLL_INTERVAL_S, interval * 1.5)


def stream_db_events_sync(org_id: str, job_id, last_seq: int = 0, max_lifetime_s: int = MAX_LIFETIME_S, *,
                          fetch=None, sleep=time.sleep, clock=time.monotonic,
                          heartbeat_s: float = HEARTBEAT_S, poll_s: float = POLL_INTERVAL_S):
    """WSGI twin of `stream_db_events`: same frames, back-off, keep-alives and lifetime; queries run in the
    request thread (one short tenant transaction each)."""
    fetch = fetch or _fetch_db_events
    deadline = clock() + max_lifetime_s
    last_beat = clock()
    interval = poll_s
    yield "retry: 3000\n\n"
    while clock() < deadline:
        rows = fetch(org_id, job_id, last_seq)
        if rows:
            interval = poll_s
            last_beat = clock()
            for seq, event, data in rows:
                last_seq = seq
                yield sse_format(str(seq), event, json.dumps(data, default=str))
                if event in TERMINAL:
                    return
            continue
        if clock() - last_beat >= heartbeat_s:
            last_beat = clock()
            yield ":hb\n\n"
        sleep(interval)
        interval = min(MAX_POLL_INTERVAL_S, interval * 1.5)


def is_asgi(request) -> bool:
    return isinstance(getattr(request, "_request", request), ASGIRequest)


def parse_last_seq(value: str | None) -> int:
    try:
        return max(0, int(str(value or "0").strip()))
    except ValueError:
        return 0


async def job_events(request, org: str, job_id):
    user = await request.auser()
    if not user.is_authenticated:
        return JsonResponse({"code": "not_authenticated", "status": 401}, status=401)
    org_id = await sync_to_async(_check_access)(user.pk, org, job_id)
    if org_id is None:
        return JsonResponse({"code": "not_found", "status": 404}, status=404)
    last_event_id = request.headers.get("Last-Event-ID")
    asgi = is_asgi(request)
    if events_backend() == "db":
        stream = stream_db_events if asgi else stream_db_events_sync
        body = stream(org_id, job_id, parse_last_seq(last_event_id))
    else:
        stream = stream_events if asgi else stream_events_sync
        body = stream(stream_key(org_id, job_id), last_event_id or "0-0")
    resp = StreamingHttpResponse(body, content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
