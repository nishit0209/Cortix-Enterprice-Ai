from __future__ import annotations

import logging
from uuid import UUID

from celery import shared_task

from apps.core import audit, jobs
from apps.core.tenancy.tasks import TenantTask, tenant_step

log = logging.getLogger(__name__)


@shared_task(base=TenantTask, name="apps.quality.tasks.evaluate_dataset_quality", long_running=True)
def evaluate_dataset_quality(*, org_id: str, dataset_id: str, version_id: str, job_id: str,
                             actor_id: str | None = None) -> dict | None:
    """queue: ingest. Re-evaluate the active rules of a dataset against one version."""
    from apps.datasets import services as dataset_services
    from apps.warehouse.exceptions import WarehouseError

    from . import services

    org = UUID(str(org_id))
    try:
        with tenant_step(org):
            jobs.update(job_id, status="running", progress=10, message="Reading the dataset version")
            dataset = dataset_services.get_dataset(dataset_id)
            version = dataset.versions.get(id=version_id)
            columns = dataset_services.version_columns(version)
            frame = dataset_services.load_version_frame(version)
            specs = services.load_rule_specs(dataset.id)
            jobs.update(job_id, status="running", progress=50, message="Evaluating quality rules")
        computation = services.compute(frame, columns, version.profile or {}, specs, org_id=org)
        with tenant_step(org):
            report = services.persist(version, computation)
            if dataset.current_version_id == version.id:
                type(dataset).objects.filter(id=dataset.id).update(quality_score=report.score)
            result = {"dataset_id": str(dataset.id), "version_id": str(version.id), "score": float(report.score),
                      "rules_failed": computation.summary["rules_failed"]}
            audit.record("quality.evaluate", target=version, metadata=result)
            jobs.finish(job_id, result=result)
        return result
    except WarehouseError as exc:
        code = exc.get_codes() if isinstance(exc.get_codes(), str) else "warehouse_error"
        with tenant_step(org):
            jobs.fail(job_id, code=code, message=str(exc.detail))
    except Exception:  # noqa: BLE001
        log.exception("quality evaluation failed dataset=%s", dataset_id)
        with tenant_step(org):
            jobs.fail(job_id, code="quality_evaluation_failed", message="Unexpected error while evaluating quality.")
    return None
