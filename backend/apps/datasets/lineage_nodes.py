"""Lineage node resolvers for datasets, dataset versions and uploads (registered in AppConfig.ready)."""

from __future__ import annotations

from uuid import UUID

from apps.lineage.services import NodeInfo, current_viewer

from .models import Dataset, DatasetVersion, Upload


def _readable(dataset_ids: list[UUID]) -> set[str] | None:
    """Datasets the graph's viewer may `read` (dataset ACLs); None = no viewer, no per-user filtering."""
    viewer = current_viewer()
    if viewer is None or not dataset_ids:
        return None
    from apps.governance.services import visible_dataset_ids

    return visible_dataset_ids(viewer, dataset_ids, "read")


def resolve_datasets(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    out: dict[UUID, NodeInfo] = {}
    rows = list(Dataset.objects.filter(id__in=ids).exclude(status="deleted").only("id", "name"))
    readable = _readable([d.id for d in rows])
    for ds in rows:
        visible = readable is None or str(ds.id) in readable
        out[ds.id] = NodeInfo(label=ds.name if visible else "", visible=visible)
    expandable = [i for i, info in out.items() if info.visible]
    for v in DatasetVersion.objects.filter(dataset_id__in=expandable).only("id", "dataset_id"):
        out[v.dataset_id].extra_edges.append(("dataset_version", v.id, "dataset", v.dataset_id, "version of"))
    return out


def resolve_versions(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    out: dict[UUID, NodeInfo] = {}
    rows = list(DatasetVersion.objects.filter(id__in=ids).select_related("dataset").exclude(dataset__status="deleted"))
    readable = _readable(list({v.dataset_id for v in rows}))
    for v in rows:
        if readable is not None and str(v.dataset_id) not in readable:
            out[v.id] = NodeInfo(label="", visible=False)
            continue
        out[v.id] = NodeInfo(
            label=f"{v.dataset.name} v{v.version}",
            extra_edges=[("dataset_version", v.id, "dataset", v.dataset_id, "version of")],
        )
    return out


def resolve_uploads(ids: list[UUID]) -> dict[UUID, NodeInfo]:
    return {u.id: NodeInfo(label=u.filename) for u in Upload.objects.filter(id__in=ids)}
