"""Celery base task that runs inside a tenant context.

    @shared_task(base=TenantTask)
    def ingest_upload(*, org_id: str, upload_id: str, actor_id: str | None = None): ...

`org_id` MUST be passed as a keyword argument. The task body runs inside `tenant_context(org_id)`
(one transaction). Long tasks should open their own short transactions with `tenant_step(org_id)`
inside a body that is decorated with `TenantTask.long_running = True`.
"""

from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from celery import Task
from django.db import connection

from .context import current_actor, current_org, tenant_context


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


class TenantTask(Task):
    abstract = True
    long_running = False  # True: body manages its own transactions via tenant_step()

    def __call__(self, *args, **kwargs):
        org_id = kwargs.get("org_id")
        if not org_id:
            raise ValueError(f"{self.name}: org_id kwarg is required for tenant tasks")
        org_uuid = UUID(str(org_id))
        active = current_org.get()
        if active is not None:  # eager execution inside an existing tenant context (keeps the caller's actor)
            if active != org_uuid:
                raise RuntimeError("TenantTask invoked for a different tenant than the active context")
            return self.run(*args, **kwargs)
        actor_id = kwargs.get("actor_id")
        actor = ("user", UUID(str(actor_id))) if actor_id else ("system", None)
        # Every ContextVar set here is reset in `finally`: worker threads run many tasks and must never let
        # one task's actor leak into the next one's audit attribution.
        actor_token = current_actor.set(actor)
        try:
            if self.long_running:
                with tenant_context(org_uuid):
                    _assert_org_active()
                return self.run(*args, **kwargs)
            with tenant_context(org_uuid):
                _assert_org_active()
                return self.run(*args, **kwargs)
        finally:
            current_actor.reset(actor_token)
