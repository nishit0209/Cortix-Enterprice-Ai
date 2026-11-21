"""Failure notification hook + ingest-finished listener.

`finalize_on_ingest` listens to `apps.datasets.signals.dataset_ingest_finished`: when the ingestion of a run's
output finishes, the run is finalized immediately (no countdown polling, so it works with every task runner).

`pipeline_run_failed` is sent (after commit) whenever a run fails for good. Receivers get
`org_id, run_id, pipeline_id` and run in the sender's thread; they must not raise. The built-in receiver
emails the pipeline owner when `notify_on_failure` is set (in-app notifications can connect here later).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.dispatch import Signal

from apps.core.tenancy import tenant_context

log = logging.getLogger(__name__)

pipeline_run_failed = Signal()


def notify_run_failed(*, org_id, run_id, pipeline_id) -> None:
    for receiver, response in pipeline_run_failed.send_robust(sender=None, org_id=org_id, run_id=run_id,
                                                               pipeline_id=pipeline_id):
        if isinstance(response, Exception):
            log.warning("pipeline failure receiver %r failed: %s", receiver, type(response).__name__)


def email_owner_on_failure(sender, *, org_id, run_id, pipeline_id, **kwargs) -> None:
    from django.contrib.auth import get_user_model

    from .models import PipelineRun

    with tenant_context(org_id):
        run = PipelineRun.objects.select_related("pipeline").filter(id=run_id).first()
        if run is None or not run.pipeline.notify_on_failure or not run.pipeline.owner_id:
            return
        pipeline = run.pipeline
        email = get_user_model().objects.filter(pk=pipeline.owner_id, is_active=True).values_list(
            "email", flat=True).first()
        if not email:
            return
        subject = f"[Nexora] Pipeline '{pipeline.name}' run #{run.number} failed"
        body = (
            f"The pipeline '{pipeline.name}' failed after {run.attempt} attempt(s).\n\n"
            f"Error: {run.error_code}\n{run.error_message}\n\n"
            f"Open the run in Nexora to see the step metrics and logs."
        )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - email is best effort
        log.warning("pipeline failure email not sent: %s", type(exc).__name__)


def _run_event_payload(org_id, run_id) -> dict:
    from .models import PipelineRun

    with tenant_context(org_id):
        run = PipelineRun.objects.select_related("pipeline").filter(id=run_id).first()
        if run is None:
            return {"run_id": str(run_id)}
        return {
            "run_id": str(run.id), "pipeline_id": str(run.pipeline_id), "pipeline_name": run.pipeline.name,
            "number": run.number, "status": run.status, "trigger": run.trigger, "attempt": run.attempt,
            "rows_out": run.rows_out, "error_code": run.error_code or None,
            "output_dataset_id": str(run.output_dataset_id) if run.output_dataset_id else None,
            "output_dataset_version_id": str(run.output_dataset_version_id) if run.output_dataset_version_id else None,
        }


def emit_run_failed_event(sender, *, org_id, run_id, pipeline_id, **kwargs) -> None:
    """Receiver of `pipeline_run_failed` -> automation event `pipeline.run_failed` (CONTRACT W5). Never raises."""
    from apps.datasets.automation import PIPELINE_RUN_FAILED, emit_event

    try:
        payload = _run_event_payload(org_id, run_id)
    except Exception:  # noqa: BLE001 - the event still goes out with the ids
        payload = {"run_id": str(run_id), "pipeline_id": str(pipeline_id)}
    emit_event(PIPELINE_RUN_FAILED, payload, org_id=org_id)


def emit_run_succeeded_event(*, org_id, run_id) -> None:
    """Called after the commit of a succeeded run -> automation event `pipeline.run_succeeded`. Never raises."""
    from apps.datasets.automation import PIPELINE_RUN_SUCCEEDED, emit_event

    try:
        payload = _run_event_payload(org_id, run_id)
    except Exception:  # noqa: BLE001 - the event still goes out with the ids
        payload = {"run_id": str(run_id)}
    emit_event(PIPELINE_RUN_SUCCEEDED, payload, org_id=org_id)


def finalize_on_ingest(sender, *, org_id, job_id, **kwargs) -> None:
    """Finalize the runs whose output ingestion `job_id` just finished. Never raises."""
    from . import runner

    try:
        runner.finalize_for_ingest_job(org_id, job_id)
    except Exception:  # noqa: BLE001 - the dispatcher / polling path finalizes later
        log.exception("finalizing pipeline runs for ingest job %s failed", job_id)
