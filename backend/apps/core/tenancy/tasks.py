"""Celery base tasks.

    @shared_task(base=TenantTask)
    def ingest_upload(*, org_id: str, upload_id: str, actor_id: str | None = None): ...

    @shared_task(base=NexoraTask)          # non-tenant ops task (purges, schedulers)
    def purge_job_events(): ...

`org_id` MUST be passed as a keyword argument. The task body runs inside `tenant_context(org_id)`
(one transaction). Long tasks should open their own short transactions with `tenant_step(org_id)`
inside a body that is decorated with `TenantTask.long_running = True`.

`.delay()` / `.apply_async()` honour `settings.TASK_RUNNER` (celery | eager | thread): see apps.core.runner.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from celery import Task
from django.db import connection

from apps.core.runner import RunnerMixin

from .context import current_actor, current_org, current_tenant, tenant_context


class OrgInactive(RuntimeError):
    pass


def _assert_org_active() -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT status FROM orgs_organization LIMIT 1")  # self-policy: only the current org
        row = cur.fetchone()
    if not row or row[0] != "active":
        raise OrgInactive(str(current_org.get()))


@contextmanager
def tenant_step(org_id: str | UUID):
    """A separate short tenant transaction (for long-running tasks)."""
    with tenant_context(org_id if isinstance(org_id, UUID) else UUID(str(org_id))):
        yield


class TenantTask(RunnerMixin, Task):
    abstract = True
    long_running = False  # True: body manages its own transactions via tenant_step()

    def __call__(self, *args, **kwargs):
        org_id = kwargs.get("org_id")
        if not org_id:
            raise ValueError(f"{self.name}: org_id kwarg is required for tenant tasks")
        org_uuid = UUID(str(org_id))
        active = current_org.get()
        keep_actor = False
        if active is not None:  # eager execution inside an existing tenant context (keeps the caller's actor)
            if active != org_uuid:
                raise RuntimeError("TenantTask invoked for a different tenant than the active context")
            if connection.in_atomic_block:
                return self.run(*args, **kwargs)
            # The ContextVar is set but no transaction is open (an eager task started from a
            # `transaction.on_commit` callback): the GUC is gone, so open a fresh tenant transaction.
            keep_actor = not kwargs.get("actor_id")
        actor_id = kwargs.get("actor_id")
        if keep_actor:
            actor = current_actor.get()
        else:
            actor = ("user", UUID(str(actor_id))) if actor_id else ("system", None)
        # Every ContextVar set here is reset in `finally`: worker threads run many tasks and must never let
        # one task's actor leak into the next one's audit attribution.
        actor_token = current_actor.set(actor)
        try:
            if self.long_running:
                with tenant_context(org_uuid):
                    _assert_org_active()
                if active is not None:
                    return self._run_detached(*args, **kwargs)
                return self.run(*args, **kwargs)
            with tenant_context(org_uuid):
                _assert_org_active()
                return self.run(*args, **kwargs)
        finally:
            current_actor.reset(actor_token)

    def _run_detached(self, *args, **kwargs):
        """A long-running body starts OUTSIDE any tenant context (it opens tenant_step()s itself)."""
        org_token = current_org.set(None)
        tenant_token = current_tenant.set(None)
        try:
            return self.run(*args, **kwargs)
        finally:
            current_tenant.reset(tenant_token)
            current_org.reset(org_token)


class NexoraTask(RunnerMixin, Task):
    """Base for NON-tenant ops tasks (purges, the periodic-job runner). Runs as the system actor, outside any
    tenant context; bodies that touch tenant rows open `tenant_context(org_id)` themselves."""

    abstract = True

    def __call__(self, *args, **kwargs):
        actor_token = current_actor.set(("system", None))
        try:
            return self.run(*args, **kwargs)
        finally:
            current_actor.reset(actor_token)
