"""Task runner semantics (apps.core.runner) without a database: thread / eager / celery routing, fresh context,
connection hygiene, retries through the timer, failure handling. Pools, timers and DB hooks are fakes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from celery import Task

from apps.core import runner
from apps.core.tenancy import tasks as tasks_mod
from apps.core.tenancy.context import current_actor, current_org
from apps.core.tenancy.tasks import NexoraTask, TenantTask
from nexora.celery import app as celery_app


class SyncPool:
    """Runs submissions immediately on the calling thread (records them)."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        from concurrent.futures import Future

        self.submitted.append(args)
        fut = Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001 - mirror ThreadPoolExecutor
            fut.set_exception(exc)
        return fut


class FakeTimer:
    created: list[FakeTimer] = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.daemon, self.started = interval, fn, False, False
        FakeTimer.created.append(self)

    def start(self):
        self.started = True

    def fire(self):
        self.fn()


@pytest.fixture
def thread_runner(settings, monkeypatch):
    settings.TASK_RUNNER = "thread"
    pool = SyncPool()
    FakeTimer.created = []
    monkeypatch.setattr(runner, "executor", lambda: pool)
    monkeypatch.setattr(runner, "_timer_factory", FakeTimer)
    closes = []
    monkeypatch.setattr("django.db.close_old_connections", lambda: closes.append(1))
    failures = []
    monkeypatch.setattr(runner, "mark_job_failed", lambda task, kwargs, exc: failures.append((task.name, kwargs, exc)))
    return pool, closes, failures


@pytest.fixture
def fake_tenancy(monkeypatch):
    @contextmanager
    def fake_context(org_id, **_kw):
        token = current_org.set(org_id)
        try:
            yield
        finally:
            current_org.reset(token)

    monkeypatch.setattr(tasks_mod, "tenant_context", fake_context)
    monkeypatch.setattr(tasks_mod, "_assert_org_active", lambda: None)


SEEN: dict[str, list] = {"probe": [], "tenant": [], "flaky": []}


@celery_app.task(lazy=False, base=NexoraTask, bind=True, name="core.tests.runner.probe")
def probe(self, value=None, boom=False):
    SEEN["probe"].append({"value": value, "actor": current_actor.get(), "org": current_org.get(),
                         "request_id": self.request.id, "retries": self.request.retries,
                         "in_pool": runner.in_pool_thread()})
    if boom:
        raise RuntimeError("boom")
    return value


@celery_app.task(lazy=False, base=TenantTask, name="core.tests.runner.tenant_probe")
def tenant_probe(*, org_id, actor_id=None, job_id=None, boom=False):
    SEEN["tenant"].append({"org": current_org.get(), "actor": current_actor.get()})
    if boom:
        raise ValueError("bad input")
    return str(current_org.get())


@celery_app.task(lazy=False, base=TenantTask, name="core.tests.runner.long_probe", long_running=True)
def long_probe(*, org_id):
    return current_org.get()


def _flaky_body(self, fail_times=1):
    SEEN["flaky"].append(self.request.retries)
    if len(SEEN["flaky"]) <= fail_times:
        raise self.retry(countdown=5, exc=RuntimeError("transient"))
    return "ok"


flaky = celery_app.task(lazy=False, base=NexoraTask, bind=True, name="core.tests.runner.flaky",
                        max_retries=3)(_flaky_body)


@celery_app.task(lazy=False, base=NexoraTask, bind=True, name="core.tests.runner.always_flaky", max_retries=1)
def always_flaky(self, fail_times=1):
    return _flaky_body(self, fail_times)


@celery_app.task(lazy=False, base=NexoraTask, name="core.tests.runner.outer")
def outer():
    return probe.delay("inner").get()


@celery_app.task(lazy=False, base=NexoraTask, name="core.tests.runner.thread_name")
def thread_name():
    import threading

    return threading.current_thread().name


@pytest.fixture(autouse=True)
def _reset_probes():
    for values in SEEN.values():
        values.clear()
    yield


# ------------------------------------------------------------------------------------------------ settings
def test_runner_name_is_validated(settings):
    for name in ("celery", "eager", "thread", "THREAD"):
        settings.TASK_RUNNER = name
        assert runner.runner_name() == name.lower()
    settings.TASK_RUNNER = "processes"
    with pytest.raises(RuntimeError):
        runner.runner_name()


def test_delay_seconds_from_countdown_or_eta():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert runner.delay_seconds() == 0.0
    assert runner.delay_seconds(countdown=5) == 5.0
    assert runner.delay_seconds(countdown=-3) == 0.0
    assert runner.delay_seconds(eta=now + timedelta(seconds=90), now=now) == 90.0
    assert runner.delay_seconds(eta=now - timedelta(seconds=90), now=now) == 0.0
    assert runner.delay_seconds(countdown=1, eta=now + timedelta(hours=1), now=now) == 1.0  # countdown wins
    naive = datetime(2026, 11, 20, 12, 0, 30)
    assert runner.delay_seconds(eta=naive, now=now) == 30.0  # naive eta is UTC


# ------------------------------------------------------------------------------------------------ thread runner
def test_thread_run_has_fresh_context_request_and_connection_hygiene(thread_runner):
    pool, closes, _ = thread_runner
    token = current_actor.set(("user", uuid4()))  # the submitter's actor must not leak into the task
    try:
        result = probe.delay(41)
    finally:
        current_actor.reset(token)
    assert isinstance(result, runner.ThreadResult) and result.ready() and result.get() == 41
    (seen,) = SEEN["probe"]
    assert seen["actor"] == ("system", None) and seen["org"] is None
    assert seen["request_id"] == result.id and seen["retries"] == 0 and seen["in_pool"] is True
    assert len(closes) == 2  # close_old_connections before and after the run
    assert runner.in_pool_thread() is False
    assert len(pool.submitted) == 1


def test_countdown_and_eta_go_through_a_daemon_timer(thread_runner):
    pool, _, _ = thread_runner
    result = probe.apply_async(args=(1,), countdown=30)
    (timer,) = FakeTimer.created
    assert timer.interval == 30 and timer.daemon and timer.started
    assert not pool.submitted and not result.ready()
    timer.fire()
    assert result.get() == 1 and len(pool.submitted) == 1


def test_retry_resubmits_through_the_timer_with_the_retry_count(thread_runner):
    pool, _, failures = thread_runner
    first = flaky.apply_async(kwargs={"fail_times": 2})
    assert first.get() is None  # the first attempt asked for a retry
    assert SEEN["flaky"] == [0]
    timer = FakeTimer.created[-1]
    assert timer.interval == 5  # the countdown passed to self.retry()
    timer.fire()
    assert SEEN["flaky"] == [0, 1]
    FakeTimer.created[-1].fire()
    assert SEEN["flaky"] == [0, 1, 2]
    assert len(FakeTimer.created) == 2 and not failures  # succeeded on the third attempt


def test_retries_exhausted_fail_and_mark_the_job(thread_runner):
    _, _, failures = thread_runner
    res = always_flaky.apply_async(kwargs={"fail_times": 99})
    assert res.get() is None
    FakeTimer.created[-1].fire()  # retry 1 -> max_retries (1) exceeded -> the original exception
    assert SEEN["flaky"] == [0, 1]
    assert len(FakeTimer.created) == 1  # no third attempt was scheduled
    assert failures and failures[-1][0] == "core.tests.runner.always_flaky"
    assert isinstance(failures[-1][2], RuntimeError)


def test_failures_are_logged_marked_and_propagated(thread_runner, caplog):
    _, closes, failures = thread_runner
    res = probe.apply_async(kwargs={"value": 1, "boom": True})
    with pytest.raises(RuntimeError, match="boom"):
        res.get()
    assert failures[0][1] == {"value": 1, "boom": True}
    assert "failed" in caplog.text and len(closes) == 2


def test_tenant_task_in_a_thread_runs_in_its_org_with_the_actor_restored(thread_runner, fake_tenancy):
    org, user = uuid4(), uuid4()
    res = tenant_probe.delay(org_id=str(org), actor_id=str(user))
    assert res.get() == str(org)
    assert SEEN["tenant"] == [{"org": org, "actor": ("user", user)}]
    assert current_org.get() is None and current_actor.get() == ("system", None)


def test_tenant_task_failure_marks_its_job(thread_runner, fake_tenancy):
    _, _, failures = thread_runner
    job = str(uuid4())
    res = tenant_probe.delay(org_id=str(uuid4()), job_id=job, boom=True)
    with pytest.raises(ValueError):
        res.get(timeout=1)
    assert failures[0][1]["job_id"] == job
    assert res.get(propagate=False).__class__ is ValueError


def test_nested_submit_from_a_pool_thread_runs_inline(thread_runner):
    pool, _, _ = thread_runner

    assert outer.delay().get() == "inner"
    assert len(pool.submitted) == 1  # only the outer task went to the pool
    assert SEEN["probe"][0]["in_pool"] is True


def test_shutdown_recreates_the_pool(settings):
    settings.TASK_RUNNER_THREADS = 2
    runner.shutdown(wait=True)
    first = runner.executor()
    assert first._max_workers == 2
    assert runner.executor() is first
    runner.shutdown(wait=True)
    assert runner.executor() is not first
    runner.shutdown(wait=True)


def test_real_pool_thread_round_trip(settings, monkeypatch):
    """The real ThreadPoolExecutor: the task runs on a nexora-task thread and its result comes back."""
    settings.TASK_RUNNER = "thread"
    settings.TASK_RUNNER_THREADS = 1
    monkeypatch.setattr("django.db.close_old_connections", lambda: None)
    runner.shutdown(wait=True)

    try:
        assert thread_name.delay().get(timeout=10).startswith(runner.THREAD_NAME_PREFIX)
    finally:
        runner.shutdown(wait=True)


# ------------------------------------------------------------------------------------------------ eager / celery
def test_eager_runs_inline_and_returns_an_eager_result(settings):
    settings.TASK_RUNNER = "eager"
    res = probe.delay(7)
    assert res.get() == 7 and SEEN["probe"][0]["in_pool"] is False
    with pytest.raises(RuntimeError):
        probe.delay(1, boom=True)  # eager propagates


def test_eager_ignores_countdown_and_honours_retries(settings):
    settings.TASK_RUNNER = "eager"
    assert flaky.apply_async(kwargs={"fail_times": 1}, countdown=60).get() == "ok"
    assert SEEN["flaky"] == [0, 1]


def test_celery_runner_delegates_to_celery(settings, monkeypatch):
    settings.TASK_RUNNER = "celery"
    calls = []
    monkeypatch.setattr(Task, "apply_async", lambda self, *a, **k: calls.append((self.name, a, k)) or "sent")
    assert probe.apply_async(args=(1,), countdown=3) == "sent"
    assert calls[0][0] == probe.name and calls[0][2]["countdown"] == 3


def test_tenant_task_from_on_commit_opens_its_own_transaction(monkeypatch, fake_tenancy):
    """An eager TenantTask started by transaction.on_commit sees the caller's org ContextVar but no open
    transaction: it must enter its own tenant context (and keep the caller's actor) instead of running bare."""
    from types import SimpleNamespace

    entered = []

    @contextmanager
    def recording_context(org_id, **_kw):
        entered.append(org_id)
        token = current_org.set(org_id)
        try:
            yield
        finally:
            current_org.reset(token)

    monkeypatch.setattr(tasks_mod, "tenant_context", recording_context)
    monkeypatch.setattr(tasks_mod, "connection", SimpleNamespace(in_atomic_block=False))
    org, user = uuid4(), uuid4()
    org_token, actor_token = current_org.set(org), current_actor.set(("user", user))
    try:
        assert tenant_probe(org_id=str(org)) == str(org)
    finally:
        current_actor.reset(actor_token)
        current_org.reset(org_token)
    assert entered == [org] and SEEN["tenant"][-1]["actor"] == ("user", user)


def test_long_running_tenant_task_from_on_commit_starts_outside_the_tenant(monkeypatch, fake_tenancy):
    from types import SimpleNamespace

    monkeypatch.setattr(tasks_mod, "connection", SimpleNamespace(in_atomic_block=False))
    org = uuid4()
    token = current_org.set(org)
    try:
        assert long_probe(org_id=str(org)) is None  # the body opens tenant_step()s itself
        assert current_org.get() == org
    finally:
        current_org.reset(token)


def test_mark_job_failed_needs_job_and_org(monkeypatch):
    calls = []
    monkeypatch.setattr("apps.core.jobs.fail", lambda *a, **k: calls.append((a, k)))
    runner.mark_job_failed(probe, {"value": 1}, RuntimeError("x"))  # no job_id/org_id: nothing to mark
    assert calls == []
