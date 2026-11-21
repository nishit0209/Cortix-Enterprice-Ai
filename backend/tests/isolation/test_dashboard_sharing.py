"""Dashboards / SavedQueries `is_shared` visibility (CONTRACT "W3 as-built additions: Dashboards: `is_shared`").

The as-built rule, encoded exactly:
  * Dashboards and SavedQueries are PRIVATE to their owner unless `is_shared=true`;
  * `is_shared` is accepted on POST/PATCH, is ALWAYS present in list and detail payloads, and defaults to `false`;
  * a member sees their own items plus shared ones; any other item is 404 (detail, list membership, widget data);
  * un-sharing hides the item again; only the owner may change it (shared: 403/404, private: 404);
  * widget data always re-executes under the VIEWER's policy (covered by tests/integration/
    test_dashboard_widget_policy.py; here we check only that a non-visible board's data is 404).

Complements tests/isolation/test_w3_isolation.py (cross-org + basic privacy). Skips until the routes exist.
"""

from __future__ import annotations

import pytest

from testing.client import api_client_for, tenant_route_exists
from testing.flows import items

pytestmark = pytest.mark.django_db

needs_dashboards = pytest.mark.skipif(
    not tenant_route_exists("dashboards"), reason="/orgs/<org>/dashboards not routed yet (Backend W3)"
)
needs_saved_queries = pytest.mark.skipif(
    not tenant_route_exists("saved-queries"), reason="/orgs/<org>/saved-queries not routed yet"
)


def _base(world) -> str:
    return f"/api/v1/orgs/{world.slug}"


def _listed_ids(client, url: str) -> dict[str, dict]:
    resp = client.get(url)
    assert resp.status_code == 200, resp.content[:300]
    body = resp.json()
    assert {"items", "next_cursor", "previous_cursor"} <= set(body), "list endpoints use the cursor envelope"
    return {str(i["id"]): i for i in items(resp)}


def _create_board(world, user, title: str, **extra) -> dict:
    resp = api_client_for(user).post(f"{_base(world)}/dashboards", {"title": title, **extra}, format="json")
    assert resp.status_code == 201, resp.content[:300]
    return resp.json()


@needs_dashboards
def test_is_shared_defaults_to_false_and_is_always_in_the_payloads(two_orgs):
    a = two_orgs.a
    board = _create_board(a, a.owner, "No flag given")
    assert "is_shared" in board and board["is_shared"] is False, board
    owner = api_client_for(a.owner)
    detail = owner.get(f"{_base(a)}/dashboards/{board['id']}").json()
    assert detail["is_shared"] is False
    listed = _listed_ids(owner, f"{_base(a)}/dashboards")
    assert listed[board["id"]]["is_shared"] is False
    for item in listed.values():
        assert isinstance(item.get("is_shared"), bool), f"list item without is_shared: {item}"
    shared = _create_board(a, a.owner, "Flag given", is_shared=True)
    assert shared["is_shared"] is True


@needs_dashboards
def test_a_member_lists_exactly_their_own_plus_shared_boards(two_orgs):
    a = two_orgs.a
    owner_private = _create_board(a, a.owner, "Owner private")
    owner_shared = _create_board(a, a.owner, "Owner shared", is_shared=True)
    analyst_private = _create_board(a, a.analyst, "Analyst private")
    analyst_shared = _create_board(a, a.analyst, "Analyst shared", is_shared=True)
    created = {b["id"] for b in (owner_private, owner_shared, analyst_private, analyst_shared)}

    def visible(user) -> set[str]:
        return set(_listed_ids(api_client_for(user), f"{_base(a)}/dashboards")) & created

    assert visible(a.owner) == {owner_private["id"], owner_shared["id"], analyst_shared["id"]}
    assert visible(a.analyst) == {analyst_private["id"], analyst_shared["id"], owner_shared["id"]}
    assert visible(a.viewer) == {owner_shared["id"], analyst_shared["id"]}
    # detail follows the same rule; a private board is 404 (never 403: its existence is not revealed)
    viewer = api_client_for(a.viewer)
    assert viewer.get(f"{_base(a)}/dashboards/{owner_shared['id']}").status_code == 200
    for private in (owner_private, analyst_private):
        resp = viewer.get(f"{_base(a)}/dashboards/{private['id']}")
        assert resp.status_code == 404, resp.status_code
        assert private["title"].encode() not in resp.content
    owner = api_client_for(a.owner)
    assert owner.get(f"{_base(a)}/dashboards/{analyst_private['id']}").status_code == 404, (
        "owning the ORG does not reveal another member's private board"
    )


@needs_dashboards
def test_unsharing_hides_the_board_again_and_only_the_owner_toggles_it(two_orgs):
    a = two_orgs.a
    board = _create_board(a, a.analyst, "Toggle me", is_shared=True)
    url = f"{_base(a)}/dashboards/{board['id']}"
    viewer, owner_of_org, author = api_client_for(a.viewer), api_client_for(a.owner), api_client_for(a.analyst)
    assert viewer.get(url).status_code == 200
    # another member (even the org owner) cannot flip the flag of a board they do not own
    resp = owner_of_org.patch(url, {"is_shared": False}, format="json")
    assert resp.status_code in (403, 404), resp.status_code
    assert author.get(url).json()["is_shared"] is True
    # the author un-shares it: it disappears for everyone else (detail and list), stays for the author
    resp = author.patch(url, {"is_shared": False}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    assert resp.json()["is_shared"] is False
    for client in (viewer, owner_of_org):
        assert client.get(url).status_code == 404
        assert board["id"] not in _listed_ids(client, f"{_base(a)}/dashboards")
    assert author.get(url).status_code == 200
    # ... and a private board cannot be flipped to shared by someone who cannot see it
    resp = owner_of_org.patch(url, {"is_shared": True}, format="json")
    assert resp.status_code == 404, resp.status_code
    assert author.get(url).json()["is_shared"] is False


@needs_dashboards
def test_widget_data_of_a_private_board_is_404_to_other_members(two_orgs):
    a = two_orgs.a
    board = _create_board(a, a.owner, "Private with widget")
    widget = api_client_for(a.owner).post(
        f"{_base(a)}/dashboards/{board['id']}/widgets",
        {"title": "Note", "kind": "text", "text": "private-board-note", "layout": {"x": 0, "y": 0, "w": 4, "h": 2}},
        format="json",
    )
    assert widget.status_code == 201, widget.content[:300]
    wid = widget.json()["id"]
    for user in (a.analyst, a.viewer):
        resp = api_client_for(user).get(f"{_base(a)}/dashboards/{board['id']}/widgets/{wid}/data")
        assert resp.status_code == 404, (user.email, resp.status_code)
        assert b"private-board-note" not in resp.content


# ------------------------------------------------------------------------------------------------ saved queries
def _saved_query(world, user, *, name: str, shared: bool | None):
    """A SavedQuery row created through the model (the POST path runs the SQL guard against the warehouse)."""
    from apps.bi.models import SavedQuery
    from apps.governance.tests.isolation import dataset_id_for

    ds = dataset_id_for(world)
    extra = {} if shared is None else {"is_shared": shared}
    with world.tenant():
        return SavedQuery.objects.create(owner_id=user.pk, dataset_id=ds, name=name, sql="SELECT 1", **extra)


@needs_saved_queries
def test_saved_queries_are_private_unless_shared(two_orgs):
    a = two_orgs.a
    default = _saved_query(a, a.owner, name="Owner default", shared=None)
    assert default.is_shared is False, "SavedQuery.is_shared must default to false"
    private = _saved_query(a, a.owner, name="Owner private", shared=False)
    shared = _saved_query(a, a.owner, name="Owner shared", shared=True)
    base = _base(a)
    owner = api_client_for(a.owner)
    own_list = _listed_ids(owner, f"{base}/saved-queries")
    assert {str(default.id), str(private.id), str(shared.id)} <= set(own_list)
    assert own_list[str(shared.id)]["is_shared"] is True and own_list[str(private.id)]["is_shared"] is False
    for user in (a.analyst, a.viewer):
        client = api_client_for(user)
        listed = _listed_ids(client, f"{base}/saved-queries")
        assert str(private.id) not in listed and str(default.id) not in listed, user.email
        for hidden in (private, default):
            resp = client.get(f"{base}/saved-queries/{hidden.id}")
            assert resp.status_code == 404, (user.email, resp.status_code)
            run = client.post(f"{base}/saved-queries/{hidden.id}/run", {}, format="json")
            assert run.status_code == 404, (user.email, run.status_code)
    # a non-owner can never change a shared one
    resp = api_client_for(a.analyst).patch(
        f"{base}/saved-queries/{shared.id}", {"name": "pwned", "is_shared": False}, format="json"
    )
    assert resp.status_code in (403, 404, 405), resp.status_code
    with a.tenant():
        shared.refresh_from_db()
    assert shared.name == "Owner shared" and shared.is_shared is True
