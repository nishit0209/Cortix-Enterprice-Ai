"""Job events on the DB backend (JOB_EVENTS_BACKEND="db", CONTRACT "Local runtime"): `jobs.publish()` writes
append-only, RLS-protected `core.JobEvent` rows with a per-job `seq`; the SSE endpoint polls them and resumes after
`Last-Event-ID` = seq; old rows are purged by the registered periodic job.

The `stream_db_events` unit tests use fake fetch/sleep/clock; everything marked `django_db` uses the real database
(nexora_app under RLS; the purge runs on the nexora_ops alias in a transactional test)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from apps.core import jobs, sse
from apps.core.tenancy import TenantContextMissing, tenant_context, tenant_scope
from apps.core.tests.fixtures import client_for, make_org, make_user, member_of, org_url
from testing.sse import parse_sse, read_stream


@pytest.fixture
def db_events(settings):
    settings.JOB_EVENTS_BACKEND = "db"


def _collect(agen) -> list[str]:
    async def run():
        return [frame async for frame in agen]

    return asyncio.run(run())


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ================================================================================================ unit (no DB)
def test_events_backend_selection(settings):
    settings.JOB_EVENTS_BACKEND = "db"
    assert jobs.events_backend() == "db"
    settings.JOB_EVENTS_BACKEND = "REDIS"
    assert jobs.events_backend() == "redis"
    settings.JOB_EVENTS_BACKEND = ""
    settings.REDIS_URL = ""
    assert jobs.events_backend() == "db"
    settings.REDIS_URL = "redis://localhost:6379/0"
    assert jobs.events_backend() == "redis"


@pytest.mark.parametrize(("header", "expected"), [
    (None, 0), ("", 0), ("7", 7), (" 12 ", 12), ("-3", 0), ("1700000000000-0", 0), ("abc", 0),
])
def test_last_event_id_is_a_sequence_number(header, expected):
    assert sse.parse_last_seq(header) == expected


def test_lock_key_is_a_stable_signed_int4():
    job = uuid4()
    key = jobs._lock_key(job)
    assert key == jobs._lock_key(str(job)) and -(2 ** 31) <= key < 2 ** 31
    assert all(-(2 ** 31) <= jobs._lock_key(uuid4()) < 2 ** 31 for _ in range(200))


def test_oversized_payloads_are_replaced_by_a_marker():
    small = {"status": "running", "progress": 10}
    assert jobs._encode(small) == '{"status": "running", "progress": 10}'
    huge = {"rows": "x" * (jobs.MAX_EVENT_BYTES + 1), "status": "running"}
    assert jobs._encode(huge) == '{"truncated": true, "keys": ["rows", "status"]}'


def test_publish_without_a_tenant_fails_closed(db_events):
    with pytest.raises(TenantContextMissing):
        jobs.publish(uuid4(), "status", {"status": "running"})


def test_db_stream_replays_after_the_last_seq_and_ends_after_done():
    calls = []
    pages = [[(3, "status", {"progress": 50}), (4, "result", {"result": {"rows": 2}})], [(5, "done", {"status": "ok"})]]

    async def fetch(org_id, job_id, after):
        calls.append(after)
        return pages.pop(0) if pages else []

    async def no_sleep(_s):
        raise AssertionError("must not sleep while rows keep coming")

    frames = _collect(sse.stream_db_events("org", "job", 2, fetch=fetch, sleep=no_sleep))
    assert frames[0] == "retry: 3000\n\n"
    assert frames[1:] == [
        'id: 3\nevent: status\ndata: {"progress": 50}\n\n',
        'id: 4\nevent: result\ndata: {"result": {"rows": 2}}\n\n',
        'id: 5\nevent: done\ndata: {"status": "ok"}\n\n',
    ]
    assert calls == [2, 4]  # each poll resumes after the last delivered seq


def test_idle_db_stream_backs_off_sends_keepalives_and_expires():
    clock, sleeps, calls = FakeClock(), [], []

    async def fetch(org_id, job_id, after):
        calls.append(after)
        return []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    frames = _collect(sse.stream_db_events("org", "job", 0, max_lifetime_s=40, fetch=fetch, sleep=sleep,
                                           clock=clock, heartbeat_s=15, poll_s=0.5))
    assert frames[0] == "retry: 3000\n\n"
    assert set(frames[1:]) == {":hb\n\n"} and len(frames[1:]) == 2  # one keep-alive per 15 s idle, 40 s lifetime
    assert sleeps[:4] == [0.5, 0.75, 1.125, 1.6875]
    assert max(sleeps) == sse.MAX_POLL_INTERVAL_S  # the poll interval backs off to its cap
    assert set(calls) == {0} and clock.now >= 40


def test_a_row_resets_the_poll_interval():
    clock, sleeps = FakeClock(), []
    pages = [[], [], [(1, "status", {})], [], [(2, "done", {})]]

    async def fetch(org_id, job_id, after):
        return pages.pop(0)

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    frames = _collect(sse.stream_db_events("org", "job", 0, fetch=fetch, sleep=sleep, clock=clock, poll_s=0.5))
    assert [f.split("\n")[0] for f in frames[1:]] == ["id: 1", "id: 2"]
    assert sleeps == [0.5, 0.75, 0.5]  # back to the base interval after a row arrived


def test_sync_db_stream_matches_the_async_one():
    """WSGI twin: identical frames, back-off, keep-alives and lifetime."""
    def run(make):
        clock, sleeps = FakeClock(), []
        pages = [[], [], [], [(1, "status", {"progress": 5})], [], [(2, "done", {"status": "ok"})]]

        def fetch(org_id, job_id, after):
            return pages.pop(0) if pages else []

        def sleep(seconds):
            sleeps.append(seconds)
            clock.now += seconds * 20  # idle long enough to need a keep-alive

        return make(fetch, sleep, clock), sleeps

    def make_sync(fetch, sleep, clock):
        return list(sse.stream_db_events_sync("org", "job", 0, fetch=fetch, sleep=sleep, clock=clock,
                                              heartbeat_s=15, poll_s=0.5))

    def make_async(fetch, sleep, clock):
        async def afetch(*args):
            return fetch(*args)

        async def asleep(seconds):
            sleep(seconds)

        return _collect(sse.stream_db_events("org", "job", 0, fetch=afetch, sleep=asleep, clock=clock,
                                             heartbeat_s=15, poll_s=0.5))

    sync_frames, sync_sleeps = run(make_sync)
    async_frames, async_sleeps = run(make_async)
    assert sync_frames == async_frames and sync_sleeps == async_sleeps
    assert sync_frames[0] == "retry: 3000\n\n" and ":hb\n\n" in sync_frames
    assert [f.split("\n")[0] for f in sync_frames if f.startswith("id:")] == ["id: 1", "id: 2"]

    clock = FakeClock()

    def idle_sleep(seconds):
        clock.now += seconds

    idle = list(sse.stream_db_events_sync("org", "job", 0, max_lifetime_s=40, fetch=lambda *a: [], sleep=idle_sleep,
                                          clock=clock, heartbeat_s=15, poll_s=0.5))
    assert idle == ["retry: 3000\n\n", ":hb\n\n", ":hb\n\n"] and clock.now >= 40  # expires like the async one


def test_sync_redis_stream_resumes_and_ends_after_done():
    class Client:
        def __init__(self):
            self.calls = []
            self.replies = [
                [],  # idle -> keep-alive
                [(b"k", [(b"5-0", {b"event": b"status", b"data": b'{"progress": 1}'})])],
                [(b"k", [(b"6-0", {b"event": b"done", b"data": b'{"status": "succeeded"}'}),
                         (b"7-0", {b"event": b"log", b"data": b"{}"})])],
            ]

        def xread(self, streams, block=None, count=None):
            self.calls.append(dict(streams))
            return self.replies.pop(0)

    client = Client()
    frames = list(sse.stream_events_sync("k", "4-0", client=client))
    assert frames == ["retry: 3000\n\n", ":hb\n\n", 'id: 5-0\nevent: status\ndata: {"progress": 1}\n\n',
                      'id: 6-0\nevent: done\ndata: {"status": "succeeded"}\n\n']  # nothing after done
    assert client.calls == [{"k": "4-0"}, {"k": "4-0"}, {"k": "5-0"}]


def _call_view(request, monkeypatch, settings, backend):
    from types import SimpleNamespace

    from asgiref.sync import async_to_sync

    settings.JOB_EVENTS_BACKEND = backend
    user = SimpleNamespace(is_authenticated=True, pk=uuid4())

    async def auser():
        return user

    request.auser = auser
    monkeypatch.setattr(sse, "_check_access", lambda *_a: str(uuid4()))
    return async_to_sync(sse.job_events)(request, "acme", uuid4())


@pytest.mark.parametrize("backend", ["db", "redis"])
def test_wsgi_requests_get_a_sync_stream_and_asgi_requests_an_async_one(backend, monkeypatch, settings):
    """Django buffers a mismatched iterator whole (no live frames, no keep-alives), so each server type gets its own."""
    from django.test import AsyncRequestFactory, RequestFactory

    wsgi = _call_view(RequestFactory().get("/events"), monkeypatch, settings, backend)
    assert wsgi.status_code == 200 and not wsgi.is_async
    assert hasattr(wsgi.streaming_content, "__next__")
    asgi = _call_view(AsyncRequestFactory().get("/events"), monkeypatch, settings, backend)
    assert asgi.status_code == 200 and asgi.is_async
    assert hasattr(asgi.streaming_content, "__aiter__")
    for resp in (wsgi, asgi):
        assert resp["Content-Type"] == "text/event-stream" and resp["X-Accel-Buffering"] == "no"
        resp.close()


def test_wsgi_db_stream_delivers_frames_live_before_done(monkeypatch, settings):
    """Under runserver (WSGI) the first frames reach the client while the job is still running."""
    from django.test import RequestFactory

    published: list[tuple[int, str, dict]] = [(1, "status", {"progress": 10})]
    polls = []

    def fetch(org_id, job_id, after):
        polls.append(after)
        if len(polls) > 5:  # a buffering iterator would poll until done/lifetime: fail fast instead of hanging
            raise AssertionError("the stream is being consumed ahead of the client")
        return [r for r in published if r[0] > after]

    monkeypatch.setattr(sse, "_fetch_db_events", fetch)
    resp = _call_view(RequestFactory().get("/events"), monkeypatch, settings, "db")
    frames = iter(resp)  # what the WSGI server does: pull one chunk at a time
    assert next(frames) == b"retry: 3000\n\n"
    assert next(frames).startswith(b"id: 1\nevent: status")  # before the job has finished
    published.append((2, "done", {"status": "succeeded"}))
    assert next(frames).startswith(b"id: 2\nevent: done")
    assert next(frames, None) is None
    assert polls == [0, 1]  # one poll per page, driven by the client's reads


# ================================================================================================ DB: publish
@pytest.mark.django_db
def test_publish_assigns_per_job_sequence_numbers(org, owner, db_events):
    from apps.core.models import JobEvent

    with tenant_context(org.id):
        job = jobs.create_job("unit.events", user_id=owner.pk)
        other = jobs.create_job("unit.events", user_id=owner.pk)
        assert [jobs.publish(job.id, "status", {"progress": p}) for p in (10, 20, 30)] == ["1", "2", "3"]
        assert jobs.publish(other.id, "status", {"progress": 1}) == "1"  # sequences are per job
        assert jobs.events_after(job.id) == [(1, "status", {"progress": 10}), (2, "status", {"progress": 20}),
                                             (3, "status", {"progress": 30})]
        assert jobs.events_after(job.id, 1, limit=1) == [(2, "status", {"progress": 20})]
        assert jobs.events_after(job.id, 3) == []
        row = JobEvent.objects.get(job_id=job.id, seq=1)
        assert row.org_id == org.id and row.event == "status" and row.created_at is not None


@pytest.mark.django_db
def test_lifecycle_helpers_write_their_events(org, owner, db_events):
    from apps.core.models import Job

    with tenant_context(org.id):
        ok = jobs.create_job("unit.ok", user_id=owner.pk)
        jobs.update(ok.id, status="running", progress=40, message="Profiling")
        jobs.finish(ok.id, result={"rows": 12})
        bad = jobs.create_job("unit.bad", user_id=owner.pk)
        jobs.fail(bad.id, code="parse_error", message="bad row 3", detail={"row": 3})
        assert [(s, e) for s, e, _ in jobs.events_after(ok.id)] == [(1, "status"), (2, "result"), (3, "done")]
        assert jobs.events_after(ok.id)[0][2] == {"status": "running", "progress": 40, "message": "Profiling"}
        assert jobs.events_after(ok.id)[2][2] == {"status": "succeeded"}
        assert jobs.events_after(bad.id) == [
            (1, "error", {"code": "parse_error", "message": "bad row 3", "detail": {"row": 3}}),
            (2, "done", {"status": "failed"}),
        ]
        assert Job.objects.get(id=ok.id).status == "succeeded" and Job.objects.get(id=bad.id).status == "failed"


@pytest.mark.django_db
def test_oversized_event_is_stored_as_a_marker(org, owner, db_events):
    with tenant_context(org.id):
        job = jobs.create_job("unit.big", user_id=owner.pk)
        jobs.publish(job.id, "log", {"blob": "x" * (jobs.MAX_EVENT_BYTES * 2)})
        assert jobs.events_after(job.id) == [(1, "log", {"truncated": True, "keys": ["blob"]})]


@pytest.mark.django_db
def test_job_events_are_tenant_isolated(org, other_org, owner, db_events):
    from apps.core.models import JobEvent

    with tenant_context(other_org.id):
        job_b = jobs.create_job("unit.b")
    with tenant_context(org.id):
        job_a = jobs.create_job("unit.a", user_id=owner.pk)
        jobs.publish(job_a.id, "log", {"line": "A-ONLY"})
        # an org-A worker that knows B's job id still writes into ITS OWN org: B's stream never sees it
        jobs.publish(job_b.id, "log", {"line": "HIJACK"})
        assert JobEvent.objects.count() == 2
    with tenant_context(other_org.id):
        assert JobEvent.objects.count() == 0
        with connection.cursor() as cur:  # RLS itself, not only the tenant manager's filter
            cur.execute("SELECT count(*) FROM core_jobevent")
            assert cur.fetchone()[0] == 0
        assert jobs.events_after(job_a.id) == [] and jobs.events_after(job_b.id) == []
        jobs.publish(job_b.id, "log", {"line": "B-ONLY"})
        assert jobs.events_after(job_b.id) == [(1, "log", {"line": "B-ONLY"})]
    with tenant_context(org.id):
        assert [d["line"] for _, _, d in jobs.events_after(job_b.id)] == ["HIJACK"]  # A's own row, A's org


@pytest.mark.django_db
def test_job_events_are_append_only_for_the_app_role(org, owner, db_events):
    from apps.core.models import JobEvent

    with tenant_context(org.id):
        job = jobs.create_job("unit.append_only", user_id=owner.pk)
        jobs.publish(job.id, "status", {"progress": 1})
        with pytest.raises(DatabaseError), transaction.atomic():
            JobEvent.objects.filter(job_id=job.id).update(event="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            JobEvent.objects.filter(job_id=job.id).delete()
        assert jobs.events_after(job.id) == [(1, "status", {"progress": 1})]


# ================================================================================================ DB: SSE
def _events_url(org, job_id) -> str:
    return org_url(org, f"jobs/{job_id}/events")


def _published_job(org, owner):
    with tenant_context(org.id):
        job = jobs.create_job("unit.sse", user_id=owner.pk)
        jobs.update(job.id, status="running", progress=10, message="Parsing")  # seq 1
        jobs.update(job.id, status="running", progress=60, message="Loading")  # seq 2
        jobs.finish(job.id, result={"rows": 42})  # seq 3 result, seq 4 done
    return job


@pytest.mark.django_db
def test_sse_streams_all_db_events_then_ends(org, owner, db_events):
    job = _published_job(org, owner)
    resp = client_for(owner).get(_events_url(org, job.id))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream") and resp["Cache-Control"] == "no-cache"
    assert resp["X-Accel-Buffering"] == "no"
    body = read_stream(resp)
    assert body.startswith("retry: 3000\n\n")
    events = parse_sse(body)
    assert [(e.id, e.event) for e in events] == [("1", "status"), ("2", "status"), ("3", "result"), ("4", "done")]
    assert events[2].data == {"result": {"rows": 42}}


@pytest.mark.django_db
def test_sse_replays_after_last_event_id(org, owner, db_events):
    job = _published_job(org, owner)
    client = client_for(owner)
    resumed = parse_sse(read_stream(client.get(_events_url(org, job.id), HTTP_LAST_EVENT_ID="2")))
    assert [(e.id, e.event) for e in resumed] == [("3", "result"), ("4", "done")]
    garbage = parse_sse(read_stream(client.get(_events_url(org, job.id), HTTP_LAST_EVENT_ID="not-a-seq")))
    assert [e.id for e in garbage] == ["1", "2", "3", "4"]  # an unusable id replays from the start


@pytest.mark.django_db
def test_sse_on_the_db_backend_is_member_and_org_scoped(org, other_org, owner, db_events):
    job = _published_job(org, owner)
    # same visibility as GET /jobs/<id>: a member follows only their own jobs unless they hold jobs.view_all
    assert client_for(member_of(org, "viewer")).get(_events_url(org, job.id)).status_code == 404
    analyst = member_of(org, "analyst")
    assert client_for(analyst).get(_events_url(org, job.id)).status_code == 404
    resp = client_for(member_of(org, "auditor")).get(_events_url(org, job.id))  # auditor: jobs.view_all
    assert resp.status_code == 200 and [e.event for e in parse_sse(read_stream(resp))][-1] == "done"
    with tenant_context(org.id):
        own = jobs.create_job("unit.own", user_id=analyst.pk)
        jobs.finish(own.id, result={"rows": 1})
    assert client_for(analyst).get(_events_url(org, own.id)).status_code == 200  # their own job
    assert client_for(None).get(_events_url(org, job.id)).status_code == 401
    with tenant_context(other_org.id):
        from apps.orgs.models import Membership

        other_owner = Membership.objects.get().user
    other = client_for(other_owner)
    assert other.get(_events_url(org, job.id)).status_code == 404  # not a member of org
    assert other.get(_events_url(other_org, job.id)).status_code == 404  # IDOR through its own slug
    assert client_for(owner).get(_events_url(org, uuid4())).status_code == 404


# ================================================================================================ DB: purge
@pytest.mark.django_db(transaction=True, databases="__all__")
def test_purge_deletes_only_events_older_than_the_retention(settings):
    """Committed rows (durable alias) purged in batches on the nexora_ops alias; recent rows survive."""
    from apps.core.models import JobEvent
    from apps.core.ops import purge_job_events

    settings.JOB_EVENTS_BACKEND = "db"
    owner = make_user(full_name="Purge Owner")
    org = make_org(owner, name="Purge Synthetic Org")
    with tenant_context(org.id):
        job = jobs.create_job("unit.purge", user_id=owner.pk)
    with tenant_scope(org.id):  # publish needs only the org ContextVar: it opens its own durable transaction
        for p in range(3):
            assert jobs.publish(job.id, "status", {"progress": p}) == str(p + 1)
    with tenant_context(org.id):
        assert JobEvent.objects.filter(job_id=job.id).count() == 3

    assert purge_job_events() == 0  # nothing is 7 days old yet
    later = timezone.now() + timedelta(days=jobs.JOB_EVENT_RETENTION_DAYS + 1)
    assert purge_job_events(now=later, batch=2) == 3  # two batches: 2 + 1
    with tenant_context(org.id):
        assert JobEvent.objects.count() == 0
