"""Task runner: how `.delay()` / `.apply_async()` execute, chosen by `settings.TASK_RUNNER` (CONTRACT "Local runtime").

    "celery"  normal Celery (needs a broker, REDIS_URL)
    "eager"   inline, in the calling thread (tests, `manage.py seed_demo`)
    "thread"  a process-wide ThreadPoolExecutor(settings.TASK_RUNNER_THREADS) inside the web process (local runtime
              with PostgreSQL only: no broker, no worker processes)

Callers never branch on the runner: `RunnerMixin` is mixed into `TenantTask` and `NexoraTask`, so
`ingest_dataset.delay(org_id=..., ...)` works the same everywhere.

A thread run:
  * starts in a FRESH contextvars.Context (no tenant / actor / request id can leak from the submitter or from the
    previous task on the same pool thread);
  * calls `close_old_connections()` before and after (pool threads are long-lived; each run gets usable
    connections and never inherits a broken one);
  * pushes a Celery request (id, args, kwargs, retries) so `self.request` and `self.retry(countdown=...)` behave
    like in a worker: a retry re-submits through a timer (`countdown` / `eta` honoured);
  * logs exceptions and marks the task's job failed (`job_id` + `org_id` kwargs) when it is not already finished.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from celery.app.trace import task_has_custom
from celery.exceptions import Ignore, Reject, Retry
from django.conf import settings

log = logging.getLogger("nexora.tasks")

RUNNERS = ("celery", "eager", "thread")
THREAD_NAME_PREFIX = "nexora-task"
_FAILED_CODE = "task_failed"
_FAILED_MESSAGE = "The background task failed unexpectedly."

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_local = threading.local()


def runner_name() -> str:
    name = str(getattr(settings, "TASK_RUNNER", "celery") or "celery").lower()
    if name not in RUNNERS:
        raise RuntimeError(f"settings.TASK_RUNNER must be one of {RUNNERS}, got {name!r}")
    return name


def executor() -> ThreadPoolExecutor:
    """The process-wide pool (created lazily, sized by TASK_RUNNER_THREADS)."""
    global _executor
    with _executor_lock:
        if _executor is None:
            workers = max(1, int(getattr(settings, "TASK_RUNNER_THREADS", 4) or 4))
            _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=THREAD_NAME_PREFIX)
        return _executor


def shutdown(wait: bool = False) -> None:
    """Stop the pool (tests / process exit). A later submit creates a new pool."""
    global _executor
    with _executor_lock:
        pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=not wait)


def in_pool_thread() -> bool:
    return bool(getattr(_local, "in_task", False))


class ThreadResult:
    """AsyncResult-like handle of a thread-runner submission (`.id`, `.ready()`, `.get(timeout=)`)."""

    def __init__(self, task_id: str, future: Future | None = None):
        self.id = self.task_id = task_id
        self._future = future if future is not None else Future()

    @property
    def future(self) -> Future:
        return self._future

    @property
    def status(self) -> str:
        if not self._future.done():
            return "PENDING"
        return "FAILURE" if self._future.exception() is not None else "SUCCESS"

    state = status

    def ready(self) -> bool:
        return self._future.done()

    def successful(self) -> bool:
        return self._future.done() and self._future.exception() is None

    def get(self, timeout: float | None = None, propagate: bool = True, **_ignored) -> Any:
        try:
            return self._future.result(timeout=timeout)
        except Exception:
            if propagate:
                raise
            return self._future.exception()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ThreadResult {self.id} {self.status}>"


def delay_seconds(countdown: float | None = None, eta: datetime | None = None, *, now: datetime | None = None) -> float:
    """Seconds to wait before running (countdown wins over eta; never negative)."""
    if countdown is not None:
        return max(0.0, float(countdown))
    if eta is not None:
        if eta.tzinfo is None:
            eta = eta.replace(tzinfo=UTC)
        return max(0.0, (eta - (now or datetime.now(UTC))).total_seconds())
    return 0.0


_timer_factory: Callable[..., Any] = threading.Timer


def submit(task, args=None, kwargs=None, *, task_id: str | None = None, countdown: float | None = None,
           eta: datetime | None = None, retries: int = 0, pool: Callable[[], Any] | None = None,
           timer_factory: Callable[..., Any] | None = None) -> ThreadResult:
    """Run `task` on the thread pool (after `countdown`/`eta` when given, through a daemon timer)."""
    pool = pool or executor
    timer_factory = timer_factory or _timer_factory
    task_id = task_id or str(uuid.uuid4())
    args = tuple(args or ())
    kwargs = dict(kwargs or {})
    result = ThreadResult(task_id)
    wait = delay_seconds(countdown, eta)

    def start() -> None:
        try:
            fut = pool().submit(_run_fresh, task, args, kwargs, task_id, retries)
        except RuntimeError as exc:  # pool shut down (process exiting)
            log.error("task %s[%s] could not be submitted: %s", task.name, task_id, exc)
            result.future.set_exception(exc)
            return
        fut.add_done_callback(lambda f: _chain(f, result.future))

    if wait > 0:
        timer = timer_factory(wait, start)
        timer.daemon = True
        timer.start()
    else:
        start()
    return result


def _chain(src: Future, dst: Future) -> None:
    if dst.done():
        return
    exc = src.exception()
    if exc is not None:
        dst.set_exception(exc)
    else:
        dst.set_result(src.result())


def _run_fresh(task, args, kwargs, task_id, retries):
    """Entry point on the pool thread: an EMPTY context, so no ContextVar of any earlier code is visible."""
    return contextvars.Context().run(run_in_thread, task, args, kwargs, task_id, retries)


def run_in_thread(task, args: tuple, kwargs: dict, task_id: str, retries: int = 0, *,
                  close_connections: Callable[[], None] | None = None,
                  mark_failed: Callable[[Any, dict, BaseException], None] | None = None):
    """Execute one task body like a worker would (request pushed, retries honoured, failures recorded)."""
    if close_connections is None:
        from django.db import close_old_connections as close_connections
    mark_failed = mark_failed or mark_job_failed
    close_connections()
    _local.in_task = True
    task.push_request(id=task_id, args=list(args), kwargs=dict(kwargs), retries=retries, is_eager=False,
                      called_directly=False, delivery_info={"is_thread_runner": True})
    # Like the Celery worker's tracer: a task with a custom __call__ (TenantTask, NexoraTask) is called, otherwise
    # its run() (Task.__call__ would push a second, "called directly" request and break retries).
    body = task if task_has_custom(task, "__call__") else task.run
    try:
        return body(*args, **kwargs)
    except Retry as exc:
        # Celery's Task.retry() already re-submitted the signature through apply_async -> submit(countdown=...).
        log.info("task %s[%s] retry %s scheduled (%s)", task.name, task_id, retries + 1, exc.humanize())
        return None
    except Ignore:
        return None
    except Reject as exc:
        log.warning("task %s[%s] rejected: %s", task.name, task_id, exc)
        mark_failed(task, kwargs, exc)
        raise
    except Exception as exc:
        log.exception("task %s[%s] failed", task.name, task_id)
        mark_failed(task, kwargs, exc)
        raise
    finally:
        task.pop_request()
        _local.in_task = False
        close_connections()


def mark_job_failed(task, kwargs: dict, exc: BaseException) -> None:
    """Mark the task's job failed (when the task carries `job_id` + `org_id` and the job is not finished yet).
    Never raises: the original exception is what matters."""
    job_id, org_id = kwargs.get("job_id"), kwargs.get("org_id")
    if not job_id or not org_id:
        return
    try:
        from . import jobs
        from .models import Job
        from .tenancy.context import tenant_context

        with tenant_context(org_id):
            if Job.objects.filter(id=job_id, status__in=("queued", "running")).exists():
                jobs.fail(job_id, code=_FAILED_CODE, message=_FAILED_MESSAGE,
                          detail={"task": task.name, "error": type(exc).__name__})
    except Exception:  # noqa: BLE001
        log.exception("task %s: could not mark job %s failed", getattr(task, "name", "?"), job_id)


class RunnerMixin:
    """Routes `.delay()` / `.apply_async()` through `settings.TASK_RUNNER` (mixed into Celery Task subclasses)."""

    def _apply_inline(self, args, kwargs, *, task_id=None, link=None, link_error=None, retries: int = 0):
        """Eager execution that also honours `self.retry()` (immediately, without the countdown); exceptions
        propagate, and retries beyond `max_retries` raise the task's exception like a worker would record it."""
        attempt = retries
        while True:
            try:
                return self.apply(args, kwargs, task_id=task_id, link=link, link_error=link_error, retries=attempt,
                                  throw=True)
            except Retry as exc:
                if exc.sig is not None:
                    args, kwargs = exc.sig.args, exc.sig.kwargs
                attempt += 1

    def apply_async(self, args=None, kwargs=None, task_id=None, producer=None, link=None, link_error=None,
                    shadow=None, **options):
        name = runner_name()
        if name == "celery":
            return super().apply_async(args, kwargs, task_id=task_id, producer=producer, link=link,
                                       link_error=link_error, shadow=shadow, **options)
        delayed = options.get("countdown") is not None or options.get("eta") is not None
        if name == "eager" or (in_pool_thread() and not delayed and not options.get("retries")):
            # In a pool thread an immediate nested submit + `.get()` could dead-lock a saturated pool: run it
            # inline instead. Delayed calls and retries (Task.retry re-sends with `retries` + countdown) always go
            # through the timer, so they never block the current pool thread.
            return self._apply_inline(args, kwargs, task_id=task_id, link=link, link_error=link_error,
                                      retries=int(options.get("retries") or 0))
        return submit(self, args, kwargs, task_id=task_id, countdown=options.get("countdown"),
                      eta=options.get("eta"), retries=int(options.get("retries") or 0))
