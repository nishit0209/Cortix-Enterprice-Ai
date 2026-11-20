"""Platform (cross-tenant) maintenance jobs. Runs on the `ops` DB alias (nexora_ops: BYPASSRLS), so it lives in
this platform module only. Nothing here is reachable from a request.

    purge_job_events(older_than_days=7)   delete core.JobEvent rows older than the retention window
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import connections, transaction
from django.utils import timezone

from .jobs import JOB_EVENT_RETENTION_DAYS
from .tenancy.tasks import NexoraTask

log = logging.getLogger("nexora.ops")

OPS_ALIAS = "ops"
PURGE_BATCH = 5000


def purge_job_events(older_than_days: int = JOB_EVENT_RETENTION_DAYS, *, batch: int = PURGE_BATCH,
                     now=None) -> int:
    """Delete job events older than `older_than_days` in batches (each batch its own short transaction).
    The append-only trigger of core_jobevent lets only nexora_ops delete. Returns the number of rows deleted."""
    cutoff = (now or timezone.now()) - timedelta(days=max(1, int(older_than_days)))
    total = 0
    while True:
        with transaction.atomic(using=OPS_ALIAS), connections[OPS_ALIAS].cursor() as cur:
            cur.execute(
                "DELETE FROM core_jobevent WHERE id IN ("
                " SELECT id FROM core_jobevent WHERE created_at < %s ORDER BY created_at LIMIT %s)",
                [cutoff, int(batch)],
            )
            deleted = cur.rowcount or 0
        total += deleted
        if deleted < batch:
            break
    if total:
        log.info("purged %s job events older than %s", total, cutoff.isoformat())
    return total


@shared_task(base=NexoraTask, name="apps.core.ops.purge_job_events_task")
def purge_job_events_task(older_than_days: int = JOB_EVENT_RETENTION_DAYS) -> int:
    return purge_job_events(older_than_days)
