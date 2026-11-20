"""Local runtime: `JOB_EVENTS_BACKEND="db"` (CONTRACT "Local runtime: Job events", Backend `apps.core`).

`core.jobs.publish()` writes `core.JobEvent` rows (org_id, job_id, seq, event, data, created_at; RLS; append-only)
and the job SSE endpoint `/orgs/<org>/jobs/<id>/events` polls them, with `Last-Event-ID` = `seq`.

Checks: per-job strictly increasing seq; rows carry the ACTIVE org (a worker of org A cannot write into org B's
job stream even with B's job id); RLS on the table; append-only; SSE replay honours Last-Event-ID; the stream ends
after `done`; a member of another org gets 404, an anonymous caller 401.

Transactional DB tests (events are written on the durable alias, outside the test transaction). Skips cleanly
until the Backend W3 `core.JobEvent` model is merged.
"""

from __future__ import annotations

import pytest
from django.apps import apps as django_apps
from django.db import DatabaseError, connection, transaction

from apps.core import jobs
from apps.core.tenancy import tenant_context
from apps.core.tenancy.operations import APPEND_ONLY_COMMENT
from testing.client import api_client_for
from testing.db import rls_status
from testing.factories import make_job
from testing.sse import parse_sse, read_stream


def _job_event_model():
    try:
        return django_apps.get_model("core", "JobEvent")
    except LookupError:
        return None


JobEvent = _job_event_model()

pytestmark = [
    pytest.mark.skipif(
        JobEvent is None, reason="core.JobEvent not implemented yet (Backend W3 local runtime)"
    ),
    pytest.mark.django_db(transaction=True, databases="__all__"),
]


@pytest.fixture
def db_events(settings):
    settings.JOB_EVENTS_BACKEND = "db"
    return settings


def _events_url(world, job_id) -> str:
    return f"/api/v1/orgs/{world.slug}/jobs/{job_id}/events"


def _rows(org_id, job_id) -> list[tuple[int, str, dict]]:
    with tenant_context(org_id):
        return list(
            JobEvent.objects.filter(job_id=job_id).order_by("seq").values_list("seq", "event", "data")
        )


def test_table_is_rls_protected_and_append_only(db):
    table = JobEvent._meta.db_table
    status = rls_status(table)
    assert status.enabled and status.forced, f"{table} must ENABLE + FORCE row level security"
    assert status.tenant_policy is not None, f"{table} has no tenant policy"
    with connection.cursor() as cur:
        cur.execute("SELECT obj_description(%s::regclass, 'pg_class')", [table])
        comment = cur.fetchone()[0] or ""
    assert comment == APPEND_ONLY_COMMENT, f"{table} must be MakeAppendOnly (comment {comment!r})"


def test_publish_appends_rows_with_increasing_seq(two_orgs, db_events):
    a = two_orgs.a
    job = make_job(a.org, user=a.owner)
    with tenant_context(a.id):
        ids = [jobs.publish(job.id, "status", {"progress": p}) for p in (10, 50, 90)]
        jobs.publish(job.id, "done", {"ok": True})
    rows = _rows(a.id, job.id)
    assert [r[0] for r in rows] == sorted({r[0] for r in rows}), "seq must be unique and increasing"
    assert len(rows) == 4 and [r[1] for r in rows] == ["status", "status", "status", "done"]
    assert [r[2]["progress"] for r in rows[:3]] == [10, 50, 90]
    assert [str(i) for i in ids] == [str(r[0]) for r in rows[:3]], "publish returns the SSE event id (= seq)"


def test_rows_are_attributed_to_the_active_org_only(two_orgs, db_events):
    a, b = two_orgs
    job_b = make_job(b.org, user=b.owner)
    with tenant_context(a.id):
        jobs.publish(job_b.id, "status", {"progress": 99, "message": "hijack"})
    assert _rows(b.id, job_b.id) == [], "an org-A publish must never land in org B's job stream"
    with tenant_context(a.id):
        foreign = list(JobEvent.objects.filter(job_id=job_b.id).values_list("org_id", flat=True))
    assert foreign in ([], [a.id]), foreign
    with tenant_context(b.id):
        assert not JobEvent.objects.filter(org_id=a.id).exists(), "RLS must hide org A's events from org B"


def test_rows_cannot_be_updated(two_orgs, db_events):
    a = two_orgs.a
    job = make_job(a.org, user=a.owner)
    with tenant_context(a.id):
        jobs.publish(job.id, "status", {"progress": 1})
    table = JobEvent._meta.db_table
    with pytest.raises(DatabaseError), tenant_context(a.id), transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(f'UPDATE "{table}" SET event = %s WHERE job_id = %s', ["done", str(job.id)])


def test_sse_replays_from_last_event_id_and_ends_after_done(two_orgs, db_events):
    a = two_orgs.a
    job = make_job(a.org, user=a.owner)
    with tenant_context(a.id):
        for p in (10, 20, 30, 40):
            jobs.publish(job.id, "status", {"progress": p})
        jobs.publish(job.id, "done", {"ok": True})
    seqs = [r[0] for r in _rows(a.id, job.id)]
    client = api_client_for(a.owner)

    full = client.get(_events_url(a, job.id), HTTP_ACCEPT="text/event-stream")
    assert full.status_code == 200
    assert full["Content-Type"].startswith("text/event-stream")
    events = parse_sse(read_stream(full))
    assert [e.id for e in events] == [str(s) for s in seqs]
    assert events[-1].event == "done"
    assert [e.data["progress"] for e in events[:4]] == [10, 20, 30, 40]

    resumed = client.get(
        _events_url(a, job.id), HTTP_ACCEPT="text/event-stream", HTTP_LAST_EVENT_ID=str(seqs[1])
    )
    assert resumed.status_code == 200
    replay = parse_sse(read_stream(resumed))
    assert [e.id for e in replay] == [str(s) for s in seqs[2:]], "Last-Event-ID must skip delivered events"
    assert [e.data.get("progress") for e in replay[:2]] == [30, 40]


def test_sse_is_isolated_across_orgs(two_orgs, db_events, anon_client):
    a, b = two_orgs
    job = make_job(a.org, user=a.owner)
    with tenant_context(a.id):
        jobs.publish(job.id, "status", {"progress": 10, "message": "secret-progress-a"})
        jobs.publish(job.id, "done", {"ok": True})
    outsider = api_client_for(b.owner)
    assert outsider.get(_events_url(a, job.id)).status_code == 404, "non-member of org A"
    assert outsider.get(_events_url(b, job.id)).status_code == 404, "A's job id through B's org"
    assert anon_client.get(_events_url(a, job.id)).status_code == 401
    # B's own stream for A's job id cannot replay A's rows either
    body = read_stream(outsider.get(_events_url(b, job.id)))
    assert "secret-progress-a" not in body
