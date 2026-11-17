"""Base classes for tenant-owned models.

Every tenant-owned model MUST:
  * subclass TenantModel (gives `org` FK + fail-closed `objects` manager)
  * add `EnableTenantRLS("<app>_<model>")` to its migration (see apps.core.tenancy.operations)
  * include "org" in every UniqueConstraint
"""

from __future__ import annotations

import threading
import uuid

from django.db import models

from .context import TenantContextMissing, TenantMismatch, current_org

_uuid7_lock = threading.Lock()
_uuid7_last = [0, 0]  # [unix_ms, 12-bit counter]


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (v7) for index-friendly primary keys.

    Monotonic within a process (RFC 9562 §6.2 method 1): ids generated in the same millisecond use an
    incrementing 12-bit counter (random start) in `rand_a`, so ordering by id breaks created_at ties
    deterministically (e.g. append-only ledgers whose "latest row" matters).
    """
    import os
    import time

    rand = int.from_bytes(os.urandom(10), "big")
    ms = int(time.time() * 1000)
    with _uuid7_lock:
        last_ms, last_seq = _uuid7_last
        if ms <= last_ms:
            ms, seq = last_ms, last_seq + 1
            if seq > 0xFFF:
                ms, seq = last_ms + 1, (rand >> 68) & 0x7FF
        else:
            seq = (rand >> 68) & 0x7FF  # random start, leaves headroom for the counter
        _uuid7_last[0], _uuid7_last[1] = ms, seq
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (seq & 0xFFF) << 64
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
