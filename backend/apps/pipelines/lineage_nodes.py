"""Lineage node resolvers for pipelines and pipeline runs (registered in AppConfig.ready)."""

from __future__ import annotations

from uuid import UUID

from apps.core.tenancy import current_tenant
from apps.lineage.services import NodeInfo

from .models import Pipeline, PipelineRun


def _can_view() -> bool:
    tenant = current_tenant.get()
    return tenant is None or not tenant.permissions or tenant.has_perm("pipelines.view")


def resolve_pipelines(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    visible = _can_view()
    return {p.id: NodeInfo(label=p.name, visible=visible)
            for p in Pipeline.objects.filter(id__in=ids).exclude(status="archived").only("id", "name")}


def resolve_runs(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    visible = _can_view()
    out: dict[UUID, NodeInfo] = {}
    for r in PipelineRun.objects.filter(id__in=ids).select_related("pipeline").only(
            "id", "number", "status", "pipeline_id", "pipeline__name"):
        out[r.id] = NodeInfo(
            label=f"{r.pipeline.name} run #{r.number} ({r.status})", visible=visible,
            extra_edges=[("pipeline", r.pipeline_id, "pipeline_run", r.id, "run")],
        )
    return out
