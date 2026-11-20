"""Public service API of apps.lineage. Other apps import only from here.

    record(from_type, from_id, to_type, to_id, run_id=None, label="", transform=None) -> LineageEdge
    graph(node_type, node_id, depth=3, viewer_id=None)
        -> {"nodes": [{id, type, label, restricted}], "edges": [{from, to, label}], "restricted_count": n}
    register_node_resolver(node_type, resolver)   # labels/expansion supplied by the owning app
    current_viewer() -> UUID | None               # for resolvers: the user the graph is built for

Node ids in the graph are the assets' UUID strings (UUIDv7, globally unique).

Restricted nodes: a resolver marks a node `visible=False` when the viewer may not see it (dataset ACL `read`,
`sources.view`, `pipelines.view`) or it no longer exists. Such nodes are never labelled, never expanded (the graph
does not traverse through them, so nothing behind a restricted asset leaks), and are collapsed into ONE placeholder
node `{id: "restricted", type: "restricted", label: "<n> restricted assets", restricted: true}` wired to the
visible neighbours. Every other node carries `restricted: false`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import UUID

from django.db.models import Q

from apps.core.tenancy import require_org_id

from .models import NODE_TYPES, LineageEdge

VALID_TYPES = {t for t, _ in NODE_TYPES}
MAX_DEPTH = 6
MAX_NODES = 500
RESTRICTED_ID = "restricted"
_viewer: ContextVar[UUID | None] = ContextVar("lineage_viewer", default=None)


def current_viewer() -> UUID | None:
    """The user a graph is being built for (None = system / no per-user filtering)."""
    return _viewer.get()


@dataclass
class NodeInfo:
    label: str
    visible: bool = True
    # extra edges to include when the node is expanded, e.g. dataset -> its versions
    extra_edges: list[tuple[str, UUID, str, UUID, str]] = field(default_factory=list)


Resolver = Callable[[list[UUID]], dict[UUID, NodeInfo]]
_resolvers: dict[str, Resolver] = {}


def register_node_resolver(node_type: str, resolver: Resolver) -> None:
    """Owning apps register how to label (and optionally expand) their nodes; missing nodes are hidden."""
    _resolvers[node_type] = resolver


def _uuid(value) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def record(
    from_type: str,
    from_id,
    to_type: str,
    to_id,
    *,
    run_id=None,
    label: str = "",
    transform: dict | None = None,
) -> LineageEdge:
    """Idempotently write an edge (inside the tenant context of the code that caused it)."""
    require_org_id()
    if from_type not in VALID_TYPES or to_type not in VALID_TYPES:
        raise ValueError(f"unknown lineage node type: {from_type!r} -> {to_type!r}")
    edge, created = LineageEdge.objects.get_or_create(
        from_type=from_type,
        from_id=_uuid(from_id),
        to_type=to_type,
        to_id=_uuid(to_id),
        run_id=_uuid(run_id) if run_id else None,
        defaults={"label": label[:120], "transform": transform},
    )
    if not created and (label and edge.label != label[:120] or transform and edge.transform != transform):
        edge.label = label[:120] or edge.label
        edge.transform = transform or edge.transform
        edge.save(update_fields=["label", "transform", "updated_at"])
    return edge


def _resolve(nodes: dict[tuple[str, UUID], NodeInfo | None]) -> None:
    by_type: dict[str, list[UUID]] = {}
    for (t, i), info in nodes.items():
        if info is None:
            by_type.setdefault(t, []).append(i)
    for t, ids in by_type.items():
        resolver = _resolvers.get(t)
        found = resolver(ids) if resolver else {}
        for i in ids:
            nodes[(t, i)] = found.get(i) or NodeInfo(label=f"{t.replace('_', ' ')}", visible=resolver is None)


def _edges_touching(keys: Iterable[tuple[str, UUID]]) -> list[LineageEdge]:
    cond = Q()
    any_key = False
    for t, i in keys:
        cond |= Q(from_type=t, from_id=i) | Q(to_type=t, to_id=i)
        any_key = True
    if not any_key:
        return []
    return list(LineageEdge.objects.filter(cond)[: MAX_NODES * 4])


def graph(node_type: str, node_id, *, depth: int = 3, viewer_id=None) -> dict:
    """Upstream + downstream graph around a node, up to `depth` hops, as `viewer_id` may see it.

    Unresolvable / restricted nodes are collapsed into one placeholder and not traversed. An invisible root gives
    an empty graph (the API answers 404)."""
    require_org_id()
    if node_type not in VALID_TYPES:
        raise ValueError(f"unknown node type {node_type!r}")
    token = _viewer.set(_uuid(viewer_id) if viewer_id else None)
    try:
        return _graph(node_type, node_id, depth)
    finally:
        _viewer.reset(token)


def _is_visible(info: NodeInfo | None) -> bool:
    return info is not None and info.visible


def _graph(node_type: str, node_id, depth: int) -> dict:
    depth = max(1, min(int(depth), MAX_DEPTH))
    start = (node_type, _uuid(node_id))
    nodes: dict[tuple[str, UUID], NodeInfo | None] = {start: None}
    edges: dict[tuple, str] = {}
    frontier = deque([start])
    _resolve(nodes)
    root = nodes[start]
    if root is None or not root.visible:
        return {"nodes": [], "edges": [], "restricted_count": 0}
    for level in range(depth):
        if not frontier or len(nodes) >= MAX_NODES:
            break
        # never expand a restricted node: its own edges would reveal what lies behind it
        current = [k for k in frontier if _is_visible(nodes.get(k))]
        frontier.clear()
        if not current:
            break
        # node-supplied structural edges (e.g. dataset -> versions)
        for key in current:
            info = nodes.get(key)
            for ft, fid, tt, tid, label in (info.extra_edges if info else []):
                for k in ((ft, fid), (tt, tid)):
                    if k not in nodes:
                        nodes[k] = None
                        frontier.append(k)
                edges[((ft, fid), (tt, tid))] = label
        for e in _edges_touching(current):
            a, b = (e.from_type, e.from_id), (e.to_type, e.to_id)
            edges[(a, b)] = e.label
            for k in (a, b):
                if k not in nodes and len(nodes) < MAX_NODES:
                    nodes[k] = None
                    frontier.append(k)
        _resolve(nodes)
        if level == depth - 1:
            break
    visible = {k for k, v in nodes.items() if v is not None and v.visible}
    hidden = {k for k in nodes if k not in visible}
    out_nodes = [{"id": str(i), "type": t, "label": nodes[(t, i)].label, "restricted": False}
                 for (t, i) in nodes if (t, i) in visible]
    out_edges = []
    restricted_id = RESTRICTED_ID
    for (a, b), label in edges.items():
        if a in visible and b in visible:
            out_edges.append({"from": str(a[1]), "to": str(b[1]), "label": label})
        elif hidden and (a in visible or b in visible):
            src = str(a[1]) if a in visible else restricted_id
            dst = str(b[1]) if b in visible else restricted_id
            out_edges.append({"from": src, "to": dst, "label": label})
    linked_hidden = {k for (a, b) in edges for k in (a, b) if k in hidden and (a in visible or b in visible)}
    if linked_hidden:
        n = len(linked_hidden)
        out_nodes.append({"id": restricted_id, "type": "restricted", "restricted": True,
                          "label": f"{n} restricted asset{'s' if n != 1 else ''}"})
    # de-duplicate edges to the placeholder
    seen = set()
    unique_edges = []
    for e in out_edges:
        key = (e["from"], e["to"], e["label"])
        if key not in seen:
            seen.add(key)
            unique_edges.append(e)
    return {"nodes": out_nodes, "edges": unique_edges, "restricted_count": len(linked_hidden)}
