"""Lineage node resolvers for datasets, dataset versions and uploads (registered in AppConfig.ready)."""

from __future__ import annotations

from uuid import UUID

from apps.lineage.services import NodeInfo

from .models import Dataset, DatasetVersion, Upload


def resolve_datasets(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    out: dict[UUID, NodeInfo] = {}
    for ds in Dataset.objects.filter(id__in=ids).exclude(status="deleted"):
        out[ds.id] = NodeInfo(label=ds.name)
    for v in DatasetVersion.objects.filter(dataset_id__in=list(out)).only("id", "dataset_id"):
        out[v.dataset_id].extra_edges.append(("dataset_version", v.id, "dataset", v.dataset_id, "version of"))
    return out


def resolve_versions(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    out: dict[UUID, NodeInfo] = {}
    for v in DatasetVersion.objects.filter(id__in=ids).select_related("dataset").exclude(dataset__status="deleted"):
        out[v.id] = NodeInfo(
            label=f"{v.dataset.name} v{v.version}",
            extra_edges=[("dataset_version", v.id, "dataset", v.dataset_id, "version of")],
        )
    return out


def resolve_uploads(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    return {u.id: NodeInfo(label=u.filename) for u in Upload.objects.filter(id__in=ids)}
