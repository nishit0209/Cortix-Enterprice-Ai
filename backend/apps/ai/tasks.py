"""apps.ai background tasks.

`reembed_pending` is the body of the periodic job "ai.reembed_pending" (see apps.ai.reembed). The scheduler tick only
ENQUEUES it, so the model download (about 1.1 GB) and the CPU encoding run in the task runner (thread pool /
Celery `ml` queue), never inside the shared scheduler thread that dispatches every other periodic job.

It is a non-tenant ops task (CONTRACT "Tasks": `NexoraTask`): it opens `tenant_context(org)` per org itself.
`NexoraTask` arrives with Backend W3; until it exists `reembed_pending` is None and apps.ai.reembed runs the job
in a daemon thread instead.
"""

from __future__ import annotations

from celery import shared_task

try:
    from apps.core.tenancy.tasks import NexoraTask
except ImportError:  # pragma: no cover - Backend W3 not merged yet
    NexoraTask = None

if NexoraTask is not None:

    @shared_task(base=NexoraTask, name="apps.ai.tasks.reembed_pending", queue="ml", ignore_result=True)
    def reembed_pending():
        from .reembed import run_reembed_pending

        return run_reembed_pending()

else:
    reembed_pending = None
