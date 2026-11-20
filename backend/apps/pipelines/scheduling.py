"""Cron schedules and the beat dispatcher.

Dispatcher pattern (MULTI_TENANCY_DESIGN §beat, DATA_ENGINEERING_DESIGN §5.4):
  * `pipelines.dispatch_due` runs every 30 s on celery beat. It uses the `ops` DB alias (nexora_ops, BYPASSRLS) for
    ONE read-only query that lists due schedule ids across orgs: `(org_id, schedule_id)` only, no tenant data.
  * Each due schedule is then claimed and fired inside that org's normal tenant context (RLS enforced) with
    `SELECT ... FOR UPDATE SKIP LOCKED`, so concurrent dispatchers never double-fire, and `next_run_at` is advanced
    with croniter in the schedule's timezone (DST-correct).
  * The same pass reaps `running` runs whose heartbeat is older than PIPELINE_HEARTBEAT_TIMEOUT_S (worker lost).
The beat entry is registered as a django_celery_beat PeriodicTask by migration `pipelines.0002_beat_dispatcher`
(data migration, idempotent), so it exists wherever migrations ran and admins can pause it without a deploy.
"""

from __future__ import annotations

import datetime as dt
import logging
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
from django.conf import settings
from django.db import connections
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.core.tenancy import tenant_context

log = logging.getLogger(__name__)

DISPATCH_TASK_NAME = "pipelines.dispatch_due"
DISPATCH_EVERY_S = 30
SCAN_LIMIT = 500


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


def scan_due(now: dt.datetime, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    """ids of due schedules (cross-org scan on the ops alias: ids only)."""
    with connections[using].cursor() as cur:
        cur.execute(SCAN_SQL, [now, limit])
        return [(UUID(str(o)), UUID(str(i))) for o, i in cur.fetchall()]


def scan_stuck(now: dt.datetime, *, using: str = "ops", limit: int = SCAN_LIMIT) -> list[tuple[UUID, UUID]]:
    cutoff = now - dt.timedelta(seconds=heartbeat_timeout_s())
    with connections[using].cursor() as cur:
        cur.execute(STUCK_SQL, [cutoff, limit])
        return [(UUID(str(o)), UUID(str(i))) for o, i in cur.fetchall()]


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


def dispatch_due(now: dt.datetime | None = None, *, using: str = "ops") -> dict[str, int]:
    now = now or timezone.now()
    counts: dict[str, int] = {}
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
