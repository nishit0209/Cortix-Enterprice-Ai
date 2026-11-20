"""Tenant context: a ContextVar for the app layer + a TRANSACTION-LOCAL Postgres setting for RLS.

Usage:
    with tenant_context(org_id):
        Dataset.objects.all()          # TenantManager filters by org AND Postgres RLS enforces it

Never set the Postgres setting at session level: pooled connections would leak it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from django.db import connections, transaction

from .constants import TENANT_GUC


class TenantContextMissing(RuntimeError):
    """Raised when tenant-scoped data is accessed without a tenant context (fail closed)."""


class TenantMismatch(RuntimeError):
    """Raised when saving a row whose org differs from the active tenant context."""


@dataclass(frozen=True)
class TenantInfo:
    org_id: UUID
    slug: str = ""
    role_key: str = ""
    permissions: frozenset[str] = field(default_factory=frozenset)

    def has_perm(self, codename: str) -> bool:
        return codename in self.permissions


current_org: ContextVar[UUID | None] = ContextVar("current_org", default=None)
current_tenant: ContextVar[TenantInfo | None] = ContextVar("current_tenant", default=None)
# (actor_type, actor_id) of whoever is acting: ("user", uuid) | ("api_key", uuid) | ("system", None)
current_actor: ContextVar[tuple[str, UUID | None]] = ContextVar("current_actor", default=("system", None))


def get_current_org_id() -> UUID | None:
    return current_org.get()


def require_org_id() -> UUID:
    org_id = current_org.get()
    if org_id is None:
        raise TenantContextMissing("No tenant context is active")
    return org_id


def set_db_tenant(org_id: UUID | str, using: str = "default") -> None:
    """Set the transaction-local tenant setting on the given connection (must be inside atomic)."""
    with connections[using].cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, str(org_id)])


def _swap_db_tenant(org_id: UUID, using: str) -> str:
    """Set the transaction-local tenant setting and return the value it had before ('' when unset)."""
    with connections[using].cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
        previous = cur.fetchone()[0] or ""
        cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, str(org_id)])
    return previous


def _restore_db_tenant(previous: str, using: str) -> None:
    conn = connections[using]
    if conn.needs_rollback:
        return  # the atomic exit rolls back (to the savepoint), which reverts the setting by itself
    with conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, previous])


@contextmanager
def tenant_context(org_id: UUID | str, *, info: TenantInfo | None = None, using: str = "default") -> Iterator[None]:
    """Enter a tenant context: ContextVar + atomic transaction + transaction-local RLS setting.

    Nested calls for the same org reuse the outer transaction (savepoint). Switching org inside an
    active context is refused to prevent accidental cross-tenant work.

    A transaction-local setting survives a savepoint RELEASE, so when this context is nested in an outer
    transaction the previous value is restored on exit; otherwise code that runs after the context in the
    same transaction would still see this tenant's rows. On an exception the savepoint/transaction
    rollback reverts it.
    """
    org_uuid = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    outer = current_org.get()
    if outer is not None and outer != org_uuid:
        raise TenantMismatch(f"Already in tenant {outer}; refusing to enter {org_uuid}")
    token = current_org.set(org_uuid)
    token_info = current_tenant.set(info or current_tenant.get() or TenantInfo(org_id=org_uuid))
    try:
        with transaction.atomic(using=using):
            previous = _swap_db_tenant(org_uuid, using)
            yield
            _restore_db_tenant(previous, using)
    finally:
        current_tenant.reset(token_info)
        current_org.reset(token)


@contextmanager
def tenant_scope(org_id: UUID | str) -> Iterator[None]:
    """ContextVar-ONLY tenant scope: no transaction, no database setting.

    For non-DB tenant work in long-running tasks, mainly object-storage I/O (`apps.core.storage` keys must be
    below `orgs/<active org>/`), so a download/upload never holds a DB transaction open:

        with tenant_scope(org_id):
            storage.download_to(key, local_path)

    Database access inside it stays fail-closed: the ORM sees the org, but without `tenant_context` the RLS
    setting is unset, so queries return no tenant rows and writes are rejected. Switching to a different org
    while another one is active is refused (like `tenant_context`)."""
    org_uuid = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
    outer = current_org.get()
    if outer is not None and outer != org_uuid:
        raise TenantMismatch(f"Already in tenant {outer}; refusing to enter {org_uuid}")
    token = current_org.set(org_uuid)
    token_info = current_tenant.set(current_tenant.get() or TenantInfo(org_id=org_uuid))
    try:
        yield
    finally:
        current_tenant.reset(token_info)
        current_org.reset(token)


__all__ = [
    "current_actor",
    "TenantContextMissing",
    "TenantInfo",
    "TenantMismatch",
    "current_org",
    "current_tenant",
    "get_current_org_id",
    "require_org_id",
    "set_db_tenant",
    "tenant_context",
    "tenant_scope",
]
