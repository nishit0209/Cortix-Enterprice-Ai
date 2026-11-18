"""Lineage node resolver for data sources (registered in AppConfig.ready)."""

from __future__ import annotations

from uuid import UUID

from apps.core.tenancy import current_tenant
from apps.lineage.services import NodeInfo

from .models import DataSource


def _can_view() -> bool:
    tenant = current_tenant.get()
    return tenant is None or not tenant.permissions or tenant.has_perm("sources.view")


def resolve_sources(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    visible = _can_view()
    return {s.id: NodeInfo(label=s.name, visible=visible)
            for s in DataSource.objects.filter(id__in=ids).only("id", "name")}
