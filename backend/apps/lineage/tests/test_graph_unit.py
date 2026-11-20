"""Lineage graph traversal and restricted-node marking (pure: edges and resolvers are faked, no DB)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.lineage import services
from apps.lineage.services import NodeInfo

A, B, C, D, E = (uuid.UUID(int=i) for i in range(1, 6))


def _edge(ft, fid, tt, tid, label=""):
    return SimpleNamespace(from_type=ft, from_id=fid, to_type=tt, to_id=tid, label=label)


@pytest.fixture
def world(monkeypatch):
    """upload A -> version B -> version C (hidden) -> version D;  C <- upload E (only reachable through C)."""
    edges = [
        _edge("upload", A, "dataset_version", B, "ingested"),
        _edge("dataset_version", B, "dataset_version", C, "derived"),
        _edge("dataset_version", C, "dataset_version", D, "derived"),
        _edge("upload", E, "dataset_version", C, "ingested"),
    ]
    hidden = {C}
    viewers = []

    def touching(keys):
        keys = set(keys)
        return [e for e in edges if (e.from_type, e.from_id) in keys or (e.to_type, e.to_id) in keys]

    def resolve(ids):
        viewers.append(services.current_viewer())
        return {i: NodeInfo(label=f"n{i.int}", visible=i not in hidden) for i in ids}

    monkeypatch.setattr(services, "require_org_id", lambda: uuid.uuid4())
    monkeypatch.setattr(services, "_edges_touching", touching)
    monkeypatch.setattr(services, "_resolvers", {"upload": resolve, "dataset_version": resolve})
    return SimpleNamespace(hidden=hidden, viewers=viewers)


def test_restricted_nodes_are_collapsed_and_not_traversed(world):
    g = services.graph("dataset_version", B, depth=6, viewer_id=uuid.UUID(int=99))
    ids = {n["id"]: n for n in g["nodes"]}
    assert set(ids) == {str(A), str(B), "restricted"}  # D and E are only reachable through the hidden C
    assert ids["restricted"] == {"id": "restricted", "type": "restricted", "label": "1 restricted asset",
                                 "restricted": True}
    assert ids[str(B)]["restricted"] is False and ids[str(B)]["label"] == "n2"
    assert {"from": str(B), "to": "restricted", "label": "derived"} in g["edges"]
    assert g["restricted_count"] == 1


def test_viewer_is_visible_to_resolvers_and_reset_afterwards(world):
    viewer = uuid.UUID(int=42)
    services.graph("dataset_version", B, viewer_id=viewer)
    assert world.viewers and set(world.viewers) == {viewer}
    assert services.current_viewer() is None


def test_hidden_root_is_an_empty_graph(world):
    assert services.graph("dataset_version", C) == {"nodes": [], "edges": [], "restricted_count": 0}


def test_without_hidden_nodes_everything_is_visible(world):
    world.hidden.clear()
    g = services.graph("dataset_version", B, depth=6)
    assert {n["id"] for n in g["nodes"]} == {str(A), str(B), str(C), str(D), str(E)}
    assert g["restricted_count"] == 0 and all(not n["restricted"] for n in g["nodes"])


def test_unknown_node_type_is_refused(world):
    with pytest.raises(ValueError):
        services.graph("bogus", A)
