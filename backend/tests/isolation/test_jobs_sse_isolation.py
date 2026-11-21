"""Jobs + SSE stream isolation (MULTI_TENANCY_DESIGN §4.7): stream keys are derived from the ACTIVE tenant."""

from __future__ import annotations

import uuid

import pytest

from apps.core import jobs
from apps.core.sse import _check_access
from apps.core.tenancy import TenantContextMissing
from testing.client import api_client_for
from testing.factories import make_job

pytestmark = pytest.mark.django_db


def test_stream_key_is_org_scoped():
    org, job = uuid.uuid4(), uuid.uuid4()
    assert jobs.stream_key(org, job) == f"org:{org}:job:{job}"
    assert jobs.stream_key(uuid.uuid4(), job) != jobs.stream_key(org, job)


def test_publish_requires_tenant_context(fake_redis):
    with pytest.raises(TenantContextMissing):
        jobs.publish(uuid.uuid4(), "status", {"status": "running"})
    assert fake_redis.streams == {}


def test_publish_writes_only_under_the_active_org_prefix(two_orgs, tenant, fake_redis):
    a, b = two_orgs
    job_b = make_job(b.org, user=b.owner)
    with tenant(a.org):
        # an A-context worker cannot write into B's stream, even when it knows B's job id
        jobs.publish(job_b.id, "status", {"status": "running"})
        jobs.update(job_b.id, status="running", progress=50, message="hijack")
        jobs.finish(job_b.id, result={"rows": 1})
    assert list(fake_redis.streams) == [jobs.stream_key(a.id, job_b.id)]
    assert jobs.stream_key(b.id, job_b.id) not in fake_redis.streams
    with tenant(b.org):
        from apps.core.models import Job

        job = Job.objects.get(id=job_b.id)
        assert job.status == "queued" and job.result is None and job.message == ""


def test_job_lifecycle_events(two_orgs, tenant, fake_redis):
    a = two_orgs.a
    with tenant(a.org):
        job = jobs.create_job("qa.lifecycle", user_id=a.owner.pk)
        jobs.update(job.id, status="running", progress=10, message="Parsing")
        jobs.fail(job.id, code="parse_error", message="bad row 3")
    events = fake_redis.events(jobs.stream_key(a.id, job.id))
    assert [e for e, _ in events] == ["status", "error", "done"]
    assert events[1][1] == {"code": "parse_error", "message": "bad row 3"}
    assert fake_redis.ttl[jobs.stream_key(a.id, job.id)] == jobs.STREAM_TTL_S


def test_idempotency_keys_are_per_org(two_orgs, tenant):
    a, b = two_orgs
    with tenant(a.org):
        ja = jobs.create_job("qa.idem", idempotency_key="same-key")
        assert jobs.create_job("qa.idem", idempotency_key="same-key").id == ja.id
    with tenant(b.org):
        jb = jobs.create_job("qa.idem", idempotency_key="same-key")
    assert ja.id != jb.id and jb.org_id == b.id


def test_sse_access_check_matrix(two_orgs):
    a, b = two_orgs
    job_a = make_job(a.org, user=a.owner)
    assert _check_access(a.owner.pk, a.slug, job_a.id) == str(a.id)
    # CONTRACT "W3 as-built: Job-event SSE": following ANOTHER user's job needs `jobs.view_all`, otherwise 404
    assert _check_access(a.viewer.pk, a.slug, job_a.id) is None
    assert _check_access(a.analyst.pk, a.slug, job_a.id) is None
    auditor = a.user("auditor")  # holds jobs.view_all
    assert _check_access(auditor.pk, a.slug, job_a.id) == str(a.id)
    own_viewer_job = make_job(a.org, user=a.viewer)
    assert _check_access(a.viewer.pk, a.slug, own_viewer_job.id) == str(a.id)  # one's own job is always followable
    assert _check_access(b.owner.pk, b.slug, job_a.id) is None  # B member, B slug, A's job
    assert _check_access(b.owner.pk, a.slug, job_a.id) is None  # not a member of A
    assert _check_access(two_orgs.outsider.pk, a.slug, job_a.id) is None
    assert _check_access(a.owner.pk, a.slug, uuid.uuid4()) is None


def test_sse_http_status_codes(two_orgs):
    a, b = two_orgs
    job_a = make_job(a.org, user=a.owner)
    url = f"/api/v1/orgs/{a.slug}/jobs/{job_a.id}/events"
    assert api_client_for(None).get(url).status_code == 401
    assert api_client_for(b.owner).get(url).status_code == 404
    assert api_client_for(b.owner).get(f"/api/v1/orgs/{b.slug}/jobs/{job_a.id}/events").status_code == 404
    assert api_client_for(a.viewer).get(url).status_code == 404  # same org, another user's job, no view_all


def test_sse_streams_only_the_requested_orgs_events(two_orgs, tenant, fake_redis):
    """WSGI test client: the stream is the sync generator on `jobs.redis_client()` (patched by `fake_redis`,
    which also pins JOB_EVENTS_BACKEND="redis"); no async iterator, so no Django warning."""
    a, b = two_orgs
    job_a = make_job(a.org, user=a.owner)
    job_b = make_job(b.org, user=b.owner)
    with tenant(b.org):  # noise in B's stream
        jobs.publish(job_b.id, "log", {"line": "B-ONLY-SECRET"})
    with tenant(a.org):
        jobs.publish(job_a.id, "status", {"status": "running"})
        jobs.finish(job_a.id, result={"ok": True})
    resp = api_client_for(a.owner).get(f"/api/v1/orgs/{a.slug}/jobs/{job_a.id}/events")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/event-stream")
    assert not getattr(resp, "is_async", False), "WSGI must get a sync generator (no buffering)"
    body = b"".join(resp).decode()
    assert "event: status" in body and "event: result" in body and "event: done" in body
    assert "B-ONLY-SECRET" not in body


def test_job_list_respects_view_all_permission(two_orgs):
    a = two_orgs.a
    own = make_job(a.org, user=a.analyst, type="qa.analyst")
    other = make_job(a.org, user=a.owner, type="qa.owner")
    resp = api_client_for(a.analyst).get(f"/api/v1/orgs/{a.slug}/jobs")
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {str(own.id)}
    assert api_client_for(a.analyst).get(f"/api/v1/orgs/{a.slug}/jobs/{other.id}").status_code == 404
    auditor = a.user("auditor")  # has jobs.view_all
    ids = {i["id"] for i in api_client_for(auditor).get(f"/api/v1/orgs/{a.slug}/jobs").json()["items"]}
    assert ids == {str(own.id), str(other.id)}
