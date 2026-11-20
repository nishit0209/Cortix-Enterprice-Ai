"""Failure notification hook.

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
