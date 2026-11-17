"""Base classes for tenant-owned models.

Every tenant-owned model MUST:
  * subclass TenantModel (gives `org` FK + fail-closed `objects` manager)
  * add `EnableTenantRLS("<app>_<model>")` to its migration (see apps.core.tenancy.operations)
  * include "org" in every UniqueConstraint
"""

from __future__ import annotations

import uuid

from django.db import models

from .context import TenantContextMissing, TenantMismatch, current_org


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (v7) for index-friendly primary keys."""
    import os
    import time

    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (rand >> 68 & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)


class TenantQuerySet(models.QuerySet):
    pass


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):  # type: ignore[misc]
    """Fail-closed manager: no tenant context -> exception, never 'all rows'."""

    def get_queryset(self):  # noqa: D401
        org_id = current_org.get()
        if org_id is None:
            raise TenantContextMissing(f"{self.model.__name__}: tenant context required")
        return super().get_queryset().filter(org_id=org_id)


class UnscopedManager(models.Manager):
    """ONLY for platform modules (apps.platform_admin, core ops). Banned elsewhere by CI."""


class TimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(TimestampedModel):
    org = models.ForeignKey("orgs.Organization", on_delete=models.RESTRICT, related_name="+", db_index=True)

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        abstract = True
        base_manager_name = "unscoped"

    def save(self, *args, **kwargs):
        active = current_org.get()
        if active is None:
            raise TenantContextMissing(f"{type(self).__name__}.save() requires a tenant context")
        if self.org_id is None:
            self.org_id = active
        elif self.org_id != active:
            raise TenantMismatch(f"{type(self).__name__} org {self.org_id} != active tenant {active}")
        super().save(*args, **kwargs)
