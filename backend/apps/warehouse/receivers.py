"""Signal receivers: provision the org warehouse when an org is created."""

from __future__ import annotations

import logging

from django.conf import settings

log = logging.getLogger(__name__)


def on_org_created(sender, org_id, **kwargs) -> None:
    """Provision the org warehouse; never break org creation.

    Normally enqueued on the `ingest` queue (with retries). In eager mode (tests/dev without a worker) it
    runs once inline, without the eager retry loop. If it fails, `ensure_provisioned()` provisions lazily
    on first use (ingestion, preview, queries).
    """
    from . import services
    from .tasks import provision_org

    try:
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            services.provision(org_id)
        else:
            provision_org.delay(org_id=str(org_id))
    except Exception:  # noqa: BLE001
        log.exception("warehouse provisioning failed for org %s (will be retried lazily)", org_id)
