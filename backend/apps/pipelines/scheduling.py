"""Cron schedules and the schedule dispatcher.

Dispatcher pattern (MULTI_TENANCY_DESIGN §beat, DATA_ENGINEERING_DESIGN §5.4):
  * `dispatch_due()` uses the `ops` DB alias (nexora_ops, BYPASSRLS) only for read-only scans that list ids across
    orgs: `(org_id, schedule_id | run_id)`, no tenant data.
  * Each due schedule is then claimed and fired inside that org's normal tenant context (RLS enforced) with
    `SELECT ... FOR UPDATE SKIP LOCKED`, so concurrent dispatchers never double-fire, and `next_run_at` is advanced
    with croniter in the schedule's timezone (DST-correct).
  * The same pass reaps `running` runs whose heartbeat is older than PIPELINE_HEARTBEAT_TIMEOUT_S (worker lost).
  * With the in-process thread runner (no durable delayed queue) it also starts retry attempts whose backoff has
    elapsed (`next_attempt_at <= now`) and finalizes runs whose output ingestion finished.

Who calls it (exactly one path is active, chosen by settings.TASK_RUNNER):
  * TASK_RUNNER="celery": celery beat runs task `pipelines.dispatch_due` (django_celery_beat PeriodicTask created by
    migration `pipelines.0002_beat_dispatcher`). `local_tick` is a no-op.
  * any other runner: `apps.core.scheduler.register_periodic("pipelines.dispatch", 60, local_tick)` (registered in
    AppConfig.ready); the Celery task is a no-op.
  * Belt and braces: every pass holds a Postgres advisory lock, so even two live paths/processes never dispatch
    concurrently, and each schedule/run is claimed row-by-row with SKIP LOCKED.
"""

from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from django.conf import settings
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.tenancy import tenant_context

log = logging.getLogger(__name__)

DISPATCH_TASK_NAME = "pipelines.dispatch_due"
DISPATCH_EVERY_S = 30
LOCAL_PERIODIC_NAME = "pipelines.dispatch"
LOCAL_EVERY_S = 60
SCAN_LIMIT = 500
DISPATCH_LOCK_KEY = "nexora:pipelines.dispatch"


def min_interval_s() -> int:
    return int(getattr(settings, "PIPELINE_MIN_SCHEDULE_INTERVAL_S", 300))


def heartbeat_timeout_s() -> int:
    return int(getattr(settings, "PIPELINE_HEARTBEAT_TIMEOUT_S", 900))


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError({"timezone": [f"Unknown time zone {name!r}."]}) from exc


def validate_cron(expr: str, tz: str) -> str:
    """Five-field cron (minute hour day month weekday). Too-frequent schedules are refused."""
    expr = " ".join((expr or "").split())
    if len(expr.split(" ")) != 5:
        raise ValidationError({"cron": ["Use a five-field cron expression: minute hour day month weekday."]})
    try:
        if not croniter.is_valid(expr):
            raise ValueError
        tzinfo = zone(tz)
        it = croniter(expr, dt.datetime(2026, 1, 1, tzinfo=tzinfo))
        fires = [it.get_next(dt.datetime) for _ in range(6)]
    except (ValueError, KeyError, CroniterBadCronError, CroniterBadDateError):
        raise ValidationError({"cron": ["Invalid cron expression."]}) from None
    gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:], strict=False)]
    if gaps and min(gaps) < min_interval_s():
        raise ValidationError({"cron": [f"Schedules may run at most every {min_interval_s() // 60} minutes."]})
    return expr


def next_fire(expr: str, tz: str, after: dt.datetime) -> dt.datetime:
    """The next fire time strictly after `after`, computed in `tz`, returned in UTC."""
    tzinfo = zone(tz)
    base = after.astimezone(tzinfo)
    nxt = croniter(expr, base).get_next(dt.datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tzinfo)
    return nxt.astimezone(dt.UTC)


# ---------------------------------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------------------------------
SCAN_SQL = (
    "SELECT org_id, id FROM pipelines_pipelineschedule WHERE enabled AND next_run_at IS NOT NULL "
    "AND next_run_at <= %s ORDER BY next_run_at LIMIT %s"
)
STUCK_SQL = (
    "SELECT org_id, id FROM pipelines_pipelinerun WHERE status = 'running' AND heartbeat_at IS NOT NULL "
    "AND heartbeat_at < %s ORDER BY heartbeat_at LIMIT %s"
)


# queued attempts whose backoff elapsed, plus queued runs that were never picked up (an in-process task lost on a
# server restart): no next_attempt_at and untouched for longer than the heartbeat timeout
RETRY_DUE_SQL = (
    "SELECT org_id, id FROM pipelines_pipelinerun WHERE status = 'queued' AND ("
    "(next_attempt_at IS NOT NULL AND next_attempt_at <= %s) OR (next_attempt_at IS NULL AND updated_at < %s)) "
    "ORDER BY created_at LIMIT %s"
)
FINALIZE_SQL = (
    "SELECT org_id, id FROM pipelines_pipelinerun WHERE status = 'running' AND ingest_job_id IS NOT NULL "
    "ORDER BY updated_at LIMIT %s"
)


def runner_mode() -> str:
    from .runner import runner_mode as _mode

    return _mode()


def _scan(sql: str, params: list, using: str) -> list[tuple[UUID, UUID]]:
    with connections[using].cursor() as cur:
        cur.execute(sql, params)
        return [(UUID(str(o)), UUID(str(i))) for o, i in cur.fetchall()]


def scan_due(now: dt.datetime, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    """ids of due schedules (cross-org scan on the ops alias: ids only)."""
    return _scan(SCAN_SQL, [now, limit], using)


def scan_stuck(now: dt.datetime, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    cutoff = now - dt.timedelta(seconds=heartbeat_timeout_s())
    return _scan(STUCK_SQL, [cutoff, limit], using)


def _stale_cutoff(now: dt.datetime) -> dt.datetime:
    return now - dt.timedelta(seconds=heartbeat_timeout_s())


def scan_retries(now: dt.datetime, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    return _scan(RETRY_DUE_SQL, [now, _stale_cutoff(now), limit], using)


def scan_finalizable(*, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    return _scan(FINALIZE_SQL, [limit], using)


def _org_active() -> bool:
    with connections["default"].cursor() as cur:
        cur.execute("SELECT status FROM orgs_organization LIMIT 1")  # RLS self-policy: only the current org
        row = cur.fetchone()
    return bool(row) and row[0] == "active"


def fire_schedule(org_id: UUID, schedule_id: UUID, now: dt.datetime | None = None) -> str:
    """Claim one due schedule in its tenant context and start a run. Returns what happened."""
    from . import services
    from .models import PipelineRun, PipelineSchedule

    now = now or timezone.now()
    with tenant_context(org_id):
        sched = (PipelineSchedule.objects.select_for_update(skip_locked=True).select_related("pipeline")
                 .filter(id=schedule_id).first())
        if sched is None:
            return "locked"
        if not sched.enabled or sched.next_run_at is None or sched.next_run_at > now:
            return "not_due"
        pipeline = sched.pipeline
        sched.next_run_at = next_fire(sched.cron, sched.timezone, now)
        sched.last_enqueued_at = now
        if not _org_active():
            sched.save(update_fields=["next_run_at", "last_enqueued_at", "updated_at"])
            return "org_inactive"
        if pipeline.status != "active" or pipeline.current_version_id is None:
            sched.save(update_fields=["next_run_at", "last_enqueued_at", "updated_at"])
            return "inactive"
        if PipelineRun.objects.filter(pipeline_id=pipeline.id, status__in=PipelineRun.ACTIVE).exists():
            sched.save(update_fields=["next_run_at", "last_enqueued_at", "updated_at"])
            services.audit_schedule_skip(pipeline, reason="run_active")
            return "skipped_active"
        run, _job = services.create_run(pipeline, trigger="schedule", user_id=pipeline.owner_id)
        sched.last_run_id = run.id
        sched.save(update_fields=["next_run_at", "last_enqueued_at", "last_run_id", "updated_at"])
        return "enqueued"


def reap_stuck_run(org_id: UUID, run_id: UUID, now: dt.datetime | None = None) -> str:
    from . import runner
    from .models import PipelineRun

    now = now or timezone.now()
    cutoff = now - dt.timedelta(seconds=heartbeat_timeout_s())
    with tenant_context(org_id):
        stale = PipelineRun.objects.filter(id=run_id, status="running", heartbeat_at__lt=cutoff).exists()
    if not stale:
        return "alive"
    runner.fail_or_retry(org_id, run_id, runner.RunFailure(
        "worker_lost", "The worker running this pipeline stopped responding.", retryable=True))
    return "reaped"


def start_due_retry(org_id: UUID, run_id: UUID, now: dt.datetime | None = None) -> str:
    """Thread runner: start a queued retry attempt whose backoff elapsed. `next_attempt_at` is pushed out by the
    heartbeat timeout as a lease, so a lost in-process task is re-dispatched later but never twice at once
    (execute_run itself only starts a run that is still `queued`)."""
    from .models import PipelineRun
    from .tasks import execute_pipeline_run

    now = now or timezone.now()
    with tenant_context(org_id):
        due = Q(next_attempt_at__lte=now) | Q(next_attempt_at__isnull=True, updated_at__lt=_stale_cutoff(now))
        run = PipelineRun.objects.select_for_update(skip_locked=True).filter(due, id=run_id, status="queued").first()
        if run is None:
            return "retry_not_due"
        run.next_attempt_at = now + dt.timedelta(seconds=heartbeat_timeout_s())
        run.save(update_fields=["next_attempt_at", "updated_at"])
        kwargs = {"org_id": str(org_id), "run_id": str(run.id),
                  "actor_id": str(run.triggered_by) if run.triggered_by else None}
        transaction.on_commit(lambda: execute_pipeline_run.apply_async(kwargs=kwargs))
    return "retry_started"


def _try_lock(using: str) -> bool:
    with connections[using].cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [DISPATCH_LOCK_KEY])
        return bool(cur.fetchone()[0])


def _unlock(using: str) -> None:
    try:
        with connections[using].cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", [DISPATCH_LOCK_KEY])
    except Exception:  # noqa: BLE001 - the lock dies with the session anyway
        log.warning("could not release the pipelines dispatch lock", exc_info=True)


def dispatch_due(now: dt.datetime | None = None, *, using: str = "ops", locked: bool = True) -> dict[str, int]:
    """One dispatcher pass (see module docstring). Returns counts per outcome; `{"busy": 1}` when another
    process holds the dispatch lock."""
    if locked:
        if not _try_lock(using):
            return {"busy": 1}
        try:
            return dispatch_due(now, using=using, locked=False)
        finally:
            _unlock(using)
    now = now or timezone.now()
    counts: dict[str, int] = {}
    if runner_mode() == "thread":
        for org_id, run_id in scan_finalizable(using=using):
            try:
                outcome = _finalize(org_id, run_id)
            except Exception:  # noqa: BLE001
                log.exception("pipeline finalize scan failed run=%s", run_id)
                outcome = "finalize_error"
            counts[outcome] = counts.get(outcome, 0) + 1
        for org_id, run_id in scan_retries(now, using=using):
            try:
                outcome = start_due_retry(org_id, run_id, now)
            except Exception:  # noqa: BLE001
                log.exception("pipeline retry dispatch failed run=%s", run_id)
                outcome = "retry_error"
            counts[outcome] = counts.get(outcome, 0) + 1
    for org_id, schedule_id in scan_due(now, using=using):
        try:
            outcome = fire_schedule(org_id, schedule_id, now)
        except Exception:  # noqa: BLE001 - one broken schedule/org must not stop the others
            log.exception("pipeline schedule dispatch failed schedule=%s", schedule_id)
            outcome = "error"
        counts[outcome] = counts.get(outcome, 0) + 1
    for org_id, run_id in scan_stuck(now, using=using):
        try:
            outcome = reap_stuck_run(org_id, run_id, now)
        except Exception:  # noqa: BLE001
            log.exception("pipeline stuck-run reaping failed run=%s", run_id)
            outcome = "reap_error"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _finalize(org_id: UUID, run_id: UUID) -> str:
    from .runner import finalize_run

    return f"finalize_{finalize_run(org_id=org_id, run_id=run_id)}"


def local_tick() -> dict[str, int] | None:
    """Entry point for `apps.core.scheduler` (in-process periodic thread). No-op under Celery, where beat runs the
    `pipelines.dispatch_due` task instead, so the two paths can never both dispatch.

    Connection management belongs to the caller: the core scheduler holds a session-level advisory lock on this
    thread's default connection while it runs jobs, so closing that connection here would release its lock mid-tick.
    """
    if runner_mode() == "celery":
        return None
    return dispatch_due()


def register_local_dispatcher() -> bool:
    """Register `local_tick` with apps.core.scheduler (AppConfig.ready). Missing scheduler -> warning, skipped."""
    try:
        from apps.core.scheduler import register_periodic
    except ImportError:
        log.warning("apps.core.scheduler is not available; the in-process pipeline dispatcher is not registered")
        return False
    register_periodic(LOCAL_PERIODIC_NAME, LOCAL_EVERY_S, local_tick)
    return True
