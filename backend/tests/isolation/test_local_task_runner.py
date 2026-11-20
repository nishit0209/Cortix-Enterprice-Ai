"""Local runtime: `TASK_RUNNER="thread"` (CONTRACT "Local runtime: PostgreSQL only", Backend `apps.core`).

`.delay()` / `.apply_async()` on `TenantTask` (and on the non-tenant `NexoraTask` base) submit the task to a
process-wide thread pool. Each run must:
  * get fresh DB connections (`close_old_connections` before and after) -> the connection is closed afterwards;
  * run inside `tenant_context(org_id)` with the actor restored -> GUC, ContextVars and RLS are the task's org;
  * leak nothing into the next run on the same pool (no tenant GUC / org / actor visible to a later task);
  * honour retries (`self.retry(countdown=...)` re-submits);
  * mark the job failed when the body raises (task kwargs `job_id` + `org_id`).

These are TRANSACTIONAL DB tests: the pool thread uses its own connection, so the test data must be committed.
Everything skips cleanly until the Backend W3 runner (`apps.core.tenancy.tasks.NexoraTask`) is merged.
"""

from __future__ import annotations

import importlib
import threading
import time
import uuid

import pytest
from celery import shared_task
from django.db import connection

from apps.core.tenancy import current_org, tenant_context
from apps.core.tenancy.constants import TENANT_GUC
from apps.core.tenancy.context import current_actor
from apps.core.tenancy.tasks import TenantTask
from testing.factories import make_job

NexoraTask = getattr(importlib.import_module("apps.core.tenancy.tasks"), "NexoraTask", None)
WAIT_S = 20.0

pytestmark = [
    pytest.mark.skipif(
        NexoraTask is None,
        reason="TASK_RUNNER=thread not implemented yet (Backend W3: NexoraTask + thread runner in apps.core)",
    ),
    pytest.mark.django_db(transaction=True, databases="__all__"),
]

# token -> list of observations (one per attempt); written by pool threads, read by the test thread
RUNS: dict[str, list[dict]] = {}
_RUNS_LOCK = threading.Lock()


def _observe(token: str, **extra) -> dict:
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true), pg_backend_pid()", [TENANT_GUC])
        guc, pid = cur.fetchone()
        raw_orgs: list[str] = []
        if guc:
            cur.execute("SELECT DISTINCT org_id::text FROM core_job")
            raw_orgs = sorted(r[0] for r in cur.fetchall())
    seen = {
        "thread": threading.get_ident(),
        "thread_name": threading.current_thread().name,
        "guc": guc or None,
        "pid": pid,
        "org": current_org.get(),
        "actor": current_actor.get(),
        "raw_orgs": raw_orgs,
        **extra,
    }
    with _RUNS_LOCK:
        RUNS.setdefault(token, []).append(seen)
    return seen


if NexoraTask is not None:

    @shared_task(base=TenantTask, bind=True, name="qa.runtime.tenant_probe", max_retries=3)
    def tenant_probe(self, *, org_id, token, actor_id=None, fail_times=0, boom=False, job_id=None):
        _observe(token, retries=self.request.retries)
        if boom:
            raise RuntimeError("qa: synthetic task failure")
        if self.request.retries < fail_times:
            raise self.retry(countdown=0.2, exc=RuntimeError("qa: transient failure"))
        return {"ok": True}

    @shared_task(base=NexoraTask, name="qa.runtime.ops_probe")
    def ops_probe(*, token):
        _observe(token)
        return {"ok": True}


def _wait_for(predicate, what: str, timeout: float = WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def _runs(token: str, n: int = 1) -> list[dict]:
    def ready():
        with _RUNS_LOCK:
            got = list(RUNS.get(token, []))
        return got if len(got) >= n else None

    return _wait_for(ready, f"{n} run(s) of {token}")


@pytest.fixture
def thread_runner(settings):
    settings.TASK_RUNNER = "thread"
    settings.CELERY_TASK_ALWAYS_EAGER = False
    yield settings
    with _RUNS_LOCK:
        RUNS.clear()


def test_thread_run_has_the_tenant_guc_actor_and_rls_of_its_org(two_orgs, thread_runner):
    a, b = two_orgs
    make_job(a.org)
    make_job(b.org)
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=token, actor_id=str(a.owner.pk))
    (seen,) = _runs(token)
    assert seen["thread"] != threading.get_ident(), "TASK_RUNNER=thread must not run the task inline"
    assert seen["guc"] == str(a.id)
    assert seen["org"] == a.id
    assert seen["actor"] == ("user", a.owner.pk)
    assert seen["raw_orgs"] == [str(a.id)], "RLS inside the task must show only the task's org"
    # the submitting thread is untouched
    assert current_org.get() is None


def test_nothing_leaks_into_later_runs_on_the_pool(two_orgs, thread_runner):
    a = two_orgs.a
    first = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=first, actor_id=str(a.owner.pk))
    _runs(first)
    threads = int(getattr(thread_runner, "TASK_RUNNER_THREADS", 4) or 4)
    for _ in range(threads * 2):  # enough sequential runs to reuse every pool thread
        token = uuid.uuid4().hex
        ops_probe.delay(token=token)
        (seen,) = _runs(token)
        assert seen["guc"] is None, f"tenant GUC leaked into a later task on {seen['thread_name']}"
        assert seen["org"] is None, f"tenant ContextVar leaked into a later task on {seen['thread_name']}"
        assert seen["actor"] != ("user", a.owner.pk), "the previous task's actor leaked"
    # a later TENANT task for the other org sees only its own org
    b = two_orgs.b
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(b.id), token=token)
    (seen,) = _runs(token)
    assert seen["guc"] == str(b.id) and seen["org"] == b.id
    assert seen["actor"] != ("user", a.owner.pk)


def test_each_run_closes_its_connection(two_orgs, thread_runner):
    a = two_orgs.a
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=token)
    (seen,) = _runs(token)
    pid = seen["pid"]
    with connection.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        assert cur.fetchone()[0] != pid, "the pool thread must use its own connection"

    def closed():
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", [pid])
            return cur.fetchone()[0] == 0

    _wait_for(closed, f"the task connection (backend pid {pid}) to be closed", timeout=10)


def test_retry_resubmits_through_the_runner(two_orgs, thread_runner):
    a = two_orgs.a
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=token, fail_times=2)
    runs = _runs(token, n=3)
    assert [r["retries"] for r in runs[:3]] == [0, 1, 2]
    assert all(r["guc"] == str(a.id) and r["org"] == a.id for r in runs), "retries keep the tenant context"
    time.sleep(0.6)  # no 4th attempt after the success
    assert len(RUNS[token]) == 3


def test_failure_marks_the_job_failed(two_orgs, thread_runner):
    from apps.core.models import Job

    a = two_orgs.a
    job = make_job(a.org, user=a.owner)
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=token, boom=True, job_id=str(job.id))
    _runs(token)

    def failed():
        with tenant_context(a.id):
            row = Job.objects.get(id=job.id)
            return row if row.status == "failed" else None

    row = _wait_for(failed, "the job to be marked failed")
    assert row.finished_at is not None or row.status == "failed"
    # another org cannot see or be affected by the failure
    with tenant_context(two_orgs.b.id):
        assert not Job.objects.filter(id=job.id).exists()


def test_eager_runner_still_runs_inline(two_orgs, settings):
    """TASK_RUNNER="eager" (tests, seed_demo) keeps running in the calling thread."""
    settings.TASK_RUNNER = "eager"
    a = two_orgs.a
    token = uuid.uuid4().hex
    tenant_probe.delay(org_id=str(a.id), token=token)
    with _RUNS_LOCK:
        (seen,) = RUNS.pop(token)
    assert seen["thread"] == threading.get_ident()
    assert seen["org"] == a.id
