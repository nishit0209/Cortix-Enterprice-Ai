from __future__ import annotations

import logging

from celery import shared_task

from apps.core.tenancy.tasks import TenantTask

from .exceptions import WarehouseUnavailable

log = logging.getLogger(__name__)


@shared_task(
    base=TenantTask,
    name="apps.warehouse.tasks.provision_org",
    autoretry_for=(WarehouseUnavailable,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=8,
)
def provision_org(*, org_id: str) -> dict:
    from .services import provision

    binding = provision(org_id)
    return {"wh_schema": binding.wh_schema, "reader_role": binding.reader_role}
