"""Periodic jobs registry + the in-process local scheduler (CONTRACT "Local runtime: Periodic jobs").

    # apps/<app>/apps.py -> ready():
    from apps.core.scheduler import register_periodic
    register_periodic("pipelines.dispatch_schedules", 60, dispatch_due_schedules)

How registered jobs run:
  * `settings.LOCAL_SCHEDULER=True`: ONE daemon thread per SERVER process (runserver / uvicorn / daphne /
    gunicorn / hypercorn; never migrate, tests, shell or other manage.py commands; env NEXORA_SCHEDULER=0
    disables it, NEXORA_SCHEDULER=1 forces it) ticks every TICK_S. Each tick is guarded by a Postgres
    `pg_try_advisory_lock`, so when several processes run only one of them ticks; last-run times live in the
    shared Django cache (the `nexora_cache` table locally), so a job is not re-run by another process.
  * Celery: `celery beat` gets one entry per job (`apps.core.scheduler.run_periodic(name)`), installed when
    beat starts (`beat_init`).

Job functions take no arguments, run outside any tenant context (they open `tenant_context` per org
themselves) and must be idempotent: after a restart a job may run once more than strictly due.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings

from .tenancy.tasks import NexoraTask

log = logging.getLogger("nexora.scheduler")

TICK_S = 15.0
FIRST_TICK_DELAY_S = 5.0
MIN_EVERY_S = 10
ADVISORY_LOCK_KEY = 0x4E58_5343  # "NXSC": one key for the whole scheduler
CACHE_PREFIX = "nexora:scheduler:last:"
SERVER_PROGRAMS = ("uvicorn", "daphne", "gunicorn", "hypercorn")


@dataclass(frozen=True)
class PeriodicJob:
    name: str
    every_seconds: int
    fn: Callable[[], object]


_registry: dict[str, PeriodicJob] = {}
_registry_lock = threading.Lock()


def register_periodic(name: str, every_seconds: int, fn: Callable[[], object]) -> PeriodicJob:
    """Register (or replace) a periodic job. Idempotent: registering the same name again replaces it."""
    if not name or not isinstance(name, str):
        raise ValueError("periodic job name must be a non-empty string")
    every = int(every_seconds)
    if every < MIN_EVERY_S:
        raise ValueError(f"periodic job {name!r}: every_seconds must be >= {MIN_EVERY_S}")
    if not callable(fn):
        raise TypeError(f"periodic job {name!r}: fn must be callable")
    job = PeriodicJob(name=name, every_seconds=every, fn=fn)
    with _registry_lock:
        _registry[name] = job
    return job


def unregister_periodic(name: str) -> None:
    with _registry_lock:
        _registry.pop(name, None)


def registered() -> list[PeriodicJob]:
    with _registry_lock:
        return sorted(_registry.values(), key=lambda j: j.name)


def get_job(name: str) -> PeriodicJob | None:
    with _registry_lock:
        return _registry.get(name)


# ------------------------------------------------------------------------------------------------ due times
def next_due_at(job: PeriodicJob, last_run: float | None, now: float) -> float:
    """Epoch seconds at which `job` is due next: immediately when it never ran, else last run + interval."""
    if last_run is None:
        return now
    return float(last_run) + job.every_seconds


def is_due(job: PeriodicJob, last_run: float | None, now: float) -> bool:
    return now >= next_due_at(job, last_run, now)


def due_jobs(jobs: list[PeriodicJob], last_runs: Mapping[str, float | None], now: float) -> list[PeriodicJob]:
    return [job for job in jobs if is_due(job, last_runs.get(job.name), now)]


# ------------------------------------------------------------------------------------------------ state
class LastRunStore:
    """Last-run epoch seconds per job: the shared Django cache, with an in-process fallback when the cache
    backend is unavailable (e.g. the `nexora_cache` table does not exist yet)."""

    def __init__(self):
        self._memory: dict[str, float] = {}

    def get(self, name: str) -> float | None:
        try:
            from django.core.cache import cache

            value = cache.get(CACHE_PREFIX + name)
            if value is not None:
                return float(value)
        except Exception:  # noqa: BLE001 - fall back to memory
            log.debug("scheduler: cache unavailable for get", exc_info=True)
        return self._memory.get(name)

    def set(self, name: str, value: float) -> None:
        self._memory[name] = value
        try:
            from django.core.cache import cache

            cache.set(CACHE_PREFIX + name, value, timeout=int(timedelta(days=30).total_seconds()))
        except Exception:  # noqa: BLE001
            log.debug("scheduler: cache unavailable for set", exc_info=True)


@contextmanager
def advisory_lock(key: int = ADVISORY_LOCK_KEY) -> Iterator[bool]:
    """Session-level `pg_try_advisory_lock` on this thread's default connection; yields whether it was acquired
    and always releases it. A dropped connection releases the lock by itself."""
    from django.db import connection

    acquired = False
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [key])
            acquired = bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 - no database: nobody ticks
        log.warning("scheduler: could not take the advisory lock", exc_info=True)
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", [key])
            except Exception:  # noqa: BLE001
                log.warning("scheduler: could not release the advisory lock", exc_info=True)


class Scheduler:
    """One tick = (lock acquired?) -> run every due job once, record its last run. Injectable for tests."""

    def __init__(self, *, lock: Callable[[], object] = advisory_lock, clock: Callable[[], float] = time.time,
                 store: LastRunStore | None = None, jobs: Callable[[], list[PeriodicJob]] = registered):
        self.lock = lock
        self.clock = clock
        self.store = store or LastRunStore()
        self.jobs = jobs

    def tick(self) -> list[str]:
        with self.lock() as acquired:
            if not acquired:
                return []
            ran: list[str] = []
            jobs = self.jobs()
            now = self.clock()
            last_runs = {job.name: self.store.get(job.name) for job in jobs}
            for job in due_jobs(jobs, last_runs, now):
                started = time.monotonic()
                try:
                    job.fn()
                except Exception:  # noqa: BLE001 - one failing job never stops the others
                    log.exception("periodic job %s failed", job.name)
                else:
                    log.debug("periodic job %s ran in %.2fs", job.name, time.monotonic() - started)
                self.store.set(job.name, now)  # also after a failure: no hot retry loop
                ran.append(job.name)
            return ran


# ------------------------------------------------------------------------------------------------ local thread
_thread: threading.Thread | None = None
_stop = threading.Event()
_start_lock = threading.Lock()


def is_server_process(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> bool:
    """True only for processes that serve HTTP (never migrate, tests, shell or other management commands)."""
    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    flag = str(environ.get("NEXORA_SCHEDULER", "")).strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    if not argv:
        return False
    prog = str(argv[0]).replace("\\", "/").lower()
    if "pytest" in prog or "celery" in prog:
        return False
    if any(name in prog for name in SERVER_PROGRAMS):
        return True
    if Path(prog).name in ("manage.py", "django-admin", "django-admin.exe") or prog.endswith("/django/__main__.py"):
        command = argv[1] if len(argv) > 1 else ""
        if command != "runserver":
            return False
        # runserver with the autoreloader: only the child process (RUN_MAIN=true) serves requests
        return environ.get("RUN_MAIN") == "true" or "--noreload" in argv
    return False


def _loop(scheduler: Scheduler, tick_s: float, first_delay_s: float) -> None:
    from django.db import close_old_connections

    if _stop.wait(first_delay_s):
        return
    while not _stop.is_set():
        close_old_connections()
        try:
            contextvars.Context().run(scheduler.tick)
        except Exception:  # noqa: BLE001 - the scheduler thread must survive anything
            log.exception("scheduler tick failed")
        finally:
            close_old_connections()
        if _stop.wait(tick_s):
            return


def start(*, tick_s: float = TICK_S, first_delay_s: float = FIRST_TICK_DELAY_S,
          scheduler: Scheduler | None = None) -> threading.Thread:
    """Start the daemon thread (once per process)."""
    global _thread
    with _start_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(scheduler or Scheduler(), tick_s, first_delay_s),
                                   name="nexora-scheduler", daemon=True)
        _thread.start()
        log.info("local scheduler started (%s jobs registered)", len(registered()))
        return _thread


def stop(timeout: float | None = 5.0) -> None:
    global _thread
    _stop.set()
    with _start_lock:
        thread, _thread = _thread, None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


def maybe_start() -> bool:
    """Called from CoreConfig.ready(): starts the local scheduler only where it belongs."""
    if not getattr(settings, "LOCAL_SCHEDULER", False):
        return False
    if not is_server_process():
        return False
    start()
    return True


# ------------------------------------------------------------------------------------------------ celery beat
@shared_task(base=NexoraTask, name="apps.core.scheduler.run_periodic")
def run_periodic(name: str) -> bool:
    """Celery entry point for one registered periodic job (beat schedules one per job)."""
    job = get_job(name)
    if job is None:
        log.warning("periodic job %s is not registered in this process", name)
        return False
    job.fn()
    return True


def beat_schedule() -> dict[str, dict]:
    return {
        f"nexora:{job.name}": {
            "task": "apps.core.scheduler.run_periodic",
            "schedule": timedelta(seconds=job.every_seconds),
            "args": (job.name,),
        }
        for job in registered()
    }


def install_beat_entries(sender=None, **_kwargs) -> None:
    """`celery.signals.beat_init` receiver: add the registry to beat's schedule."""
    scheduler = getattr(sender, "scheduler", None)
    if scheduler is None:
        return
    entries = beat_schedule()
    if entries:
        scheduler.update_from_dict(entries)
        log.info("celery beat: %s nexora periodic jobs installed", len(entries))
