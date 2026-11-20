"""Signal receivers: provision the org warehouse when an org is created."""

from __future__ import annotations

import logging

from django.conf import settings

log = logging.getLogger(__name__)


def on_org_created(sender, org_id, **kwargs) -> None:
    """Provision the org warehouse; never break org creation.

    Normally a background task (Celery worker or the in-process thread runner, with retries), started after the
    org row is committed so the task can see it. In eager mode (tests, `seed_demo`) it runs once inline, without
    the eager retry loop. If it fails, `ensure_provisioned()` provisions lazily on first use (ingestion, preview,
    queries).
    """
    from django.db import transaction

    from . import services
    from .tasks import provision_org

    try:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) or getattr(settings, "TASK_RUNNER", "") == "eager":
            services.provision(org_id)
        else:
            # robust: a failing dispatch is logged by Django and never breaks the committed org creation
            transaction.on_commit(lambda: provision_org.delay(org_id=str(org_id)), robust=True)
    except Exception:  # noqa: BLE001
        log.exception("warehouse provisioning failed for org %s (will be retried lazily)", org_id)
