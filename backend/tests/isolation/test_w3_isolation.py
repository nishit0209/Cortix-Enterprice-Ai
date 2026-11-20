"""W3 isolation: dashboards, widgets, widget data, notifications, private conversations (CONTRACT "W3 additions").

The URLconf-driven matrix (test_endpoint_matrix.py) already probes every new tenant route across orgs once the
owning app ships its isolation factories. This module adds what the matrix cannot express:

  * INSIDE one org, a notification belongs to one user: user B never lists, reads or marks user A's
    notifications, and `read-all` only touches the caller's own;
  * INSIDE one org, a BI conversation is private to its author (detail, messages, posting, feedback, list);
  * dashboards and widgets of org A are unreachable through org B (detail, widgets, widget data, PATCH/DELETE),
    and widget data never serves rows to a non-member.

Every block skips cleanly until its feature is routed.
"""

from __future__ import annotations

import importlib.util

import pytest

from testing.client import api_client_for, tenant_route_exists
from testing.flows import items

pytestmark = pytest.mark.django_db

HAS_NOTIFICATIONS = importlib.util.find_spec("apps.core.notifications") is not None and tenant_route_exists(
    "notifications"
)
HAS_DASHBOARDS = tenant_route_exists("dashboards")
needs_notifications = pytest.mark.skipif(
    not HAS_NOTIFICATIONS, reason="notifications not implemented yet (Backend W3 apps.core.notifications)"
)
needs_dashboards = pytest.mark.skipif(
    not HAS_DASHBOARDS, reason="/orgs/<org>/dashboards not routed yet (Backend W3)"
)


def _base(world) -> str:
    return f"/api/v1/orgs/{world.slug}"


# ------------------------------------------------------------------------------------------------ notifications
def _notify(world, users, title: str):
    from apps.core.notifications import notify

    with world.tenant():
        notify([u.pk for u in users], "qa.isolation", title, f"{title} body", "/o/x/data/datasets")


def _notification_ids(client, world, **params) -> dict[str, str]:
    resp = client.get(f"{_base(world)}/notifications", params)
    assert resp.status_code == 200, resp.content[:300]
    return {n["title"]: str(n["id"]) for n in items(resp)}


@needs_notifications
def test_notifications_are_private_to_their_user_inside_one_org(two_orgs):
    a = two_orgs.a
    _notify(a, [a.owner], "owner-only-secret")
    _notify(a, [a.analyst], "analyst-only")
    owner, analyst = api_client_for(a.owner), api_client_for(a.analyst)

    owner_ids = _notification_ids(owner, a)
    analyst_ids = _notification_ids(analyst, a)
    assert "owner-only-secret" in owner_ids and "analyst-only" not in owner_ids
    assert "analyst-only" in analyst_ids and "owner-only-secret" not in analyst_ids
    body = analyst.get(f"{_base(a)}/notifications").content.decode()
    assert owner_ids["owner-only-secret"] not in body

    # the analyst cannot mark the owner's notification read (no existence oracle: 404)
    resp = analyst.post(f"{_base(a)}/notifications/{owner_ids['owner-only-secret']}/read", {}, format="json")
    assert resp.status_code == 404, resp.content[:300]
    # read-all only touches the caller's own notifications
    assert analyst.post(f"{_base(a)}/notifications/read-all", {}, format="json").status_code in (200, 204)
    unread_owner = _notification_ids(owner, a, unread="1")
    assert "owner-only-secret" in unread_owner, "another user's read-all marked my notification read"
    assert "analyst-only" not in _notification_ids(analyst, a, unread="1")


@needs_notifications
def test_notifications_never_cross_orgs(two_orgs):
    a, b = two_orgs
    _notify(a, [a.owner], "org-a-secret")
    a_ids = _notification_ids(api_client_for(a.owner), a)
    b_owner = api_client_for(b.owner)
    assert "org-a-secret" not in _notification_ids(b_owner, b)
    nid = a_ids["org-a-secret"]
    assert b_owner.post(f"{_base(b)}/notifications/{nid}/read", {}, format="json").status_code == 404
    assert b_owner.post(f"{_base(a)}/notifications/{nid}/read", {}, format="json").status_code == 404
    assert b_owner.get(f"{_base(a)}/notifications").status_code == 404


@needs_notifications
def test_notify_requires_a_tenant_context(two_orgs):
    from apps.core.notifications import notify

    from apps.core.tenancy import TenantContextMissing

    with pytest.raises((TenantContextMissing, RuntimeError, PermissionError)):
        notify([two_orgs.a.owner.pk], "qa.isolation", "no-context", "", "")


# ------------------------------------------------------------------------------------------------ conversations
def _owner_conversation(world):
    from apps.bi.models import Conversation, Message
    from apps.governance.tests.isolation import dataset_id_for

    ds = dataset_id_for(world)
    with world.tenant():
        conv = Conversation.objects.create(user_id=world.owner.pk, dataset_id=ds, title="owner private chat")
        msg = Message.objects.create(
            conversation=conv,
            role="assistant",
            status="ok",
            question="owner secret question",
            content="owner secret answer",
            sql_raw="SELECT 1",
            sql_source="builder",
        )
    return conv, msg


@pytest.mark.skipif(not tenant_route_exists("bi/conversations"), reason="bi conversations not routed")
def test_conversations_are_private_to_their_author_inside_one_org(two_orgs):
    a = two_orgs.a
    conv, msg = _owner_conversation(a)
    for user in (a.analyst, a.viewer):
        client = api_client_for(user)
        detail = client.get(f"{_base(a)}/bi/conversations/{conv.id}")
        assert detail.status_code == 404, (user.email, detail.status_code)
        messages = client.get(f"{_base(a)}/bi/conversations/{conv.id}/messages")
        assert messages.status_code in (403, 404), messages.status_code
        assert b"owner secret" not in messages.content
        listed = client.get(f"{_base(a)}/bi/conversations")
        assert str(conv.id) not in listed.content.decode()
        post = client.post(
            f"{_base(a)}/bi/conversations/{conv.id}/messages", {"question": "hijack"}, format="json"
        )
        assert post.status_code in (403, 404), post.status_code
        feedback = client.post(f"{_base(a)}/bi/messages/{msg.id}/feedback", {"value": "down"}, format="json")
        assert feedback.status_code in (403, 404), feedback.status_code
    # the author still sees it
    assert api_client_for(a.owner).get(f"{_base(a)}/bi/conversations/{conv.id}").status_code == 200


# ------------------------------------------------------------------------------------------------ dashboards
def _dashboard(world, title: str = "Org A board") -> dict:
    resp = api_client_for(world.owner).post(f"{_base(world)}/dashboards", {"title": title}, format="json")
    assert resp.status_code == 201, resp.content[:300]
    return resp.json()


def _text_widget(world, dashboard_id: str) -> dict:
    resp = api_client_for(world.owner).post(
        f"{_base(world)}/dashboards/{dashboard_id}/widgets",
        {
            "title": "Org A note",
            "kind": "text",
            "text": "org-a-confidential-note",
            "layout": {"x": 0, "y": 0, "w": 4, "h": 2},
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content[:300]
    return resp.json()


@needs_dashboards
def test_dashboards_and_widgets_are_unreachable_from_another_org(two_orgs):
    a, b = two_orgs
    board = _dashboard(a)
    widget = _text_widget(a, board["id"])
    b_owner = api_client_for(b.owner)
    paths = [
        f"dashboards/{board['id']}",
        f"dashboards/{board['id']}/widgets",
        f"dashboards/{board['id']}/widgets/{widget['id']}",
        f"dashboards/{board['id']}/widgets/{widget['id']}/data",
    ]
    for suffix in paths:
        for base in (_base(b), _base(a)):
            resp = b_owner.get(f"{base}/{suffix}")
            assert resp.status_code in (404, 405), f"GET {base}/{suffix} -> {resp.status_code}"
            assert b"org-a-confidential-note" not in resp.content
    for method, suffix, body in (
        ("patch", f"dashboards/{board['id']}", {"title": "pwned"}),
        ("delete", f"dashboards/{board['id']}", None),
        (
            "post",
            f"dashboards/{board['id']}/widgets",
            {"title": "x", "kind": "text", "text": "x", "layout": {}},
        ),
        ("patch", f"dashboards/{board['id']}/widgets/{widget['id']}", {"text": "pwned"}),
        ("delete", f"dashboards/{board['id']}/widgets/{widget['id']}", None),
    ):
        resp = getattr(b_owner, method)(f"{_base(b)}/{suffix}", body or {}, format="json")
        assert resp.status_code == 404, f"{method.upper()} {suffix} -> {resp.status_code}"
    # nothing changed for org A
    detail = api_client_for(a.owner).get(f"{_base(a)}/dashboards/{board['id']}").json()
    assert detail["title"] == "Org A board"
    assert [w["id"] for w in detail["widgets"]] == [widget["id"]]
    listed = b_owner.get(f"{_base(b)}/dashboards").content.decode()
    assert board["id"] not in listed and widget["id"] not in listed


@needs_dashboards
def test_widgets_cannot_reference_another_orgs_saved_query_or_message(two_orgs):
    """POST /widgets with org A's saved_query_id / message_id through org B must not bind A's SQL to B's board."""
    from apps.bi.tests.isolation import _message, _saved_query

    a, b = two_orgs
    sq_a = _saved_query(a)["pk"]
    msg_a = _message(a)["pk"]
    board_b = _dashboard(b, "Org B board")
    client = api_client_for(b.owner)
    for ref in ({"saved_query_id": sq_a}, {"message_id": msg_a}):
        resp = client.post(
            f"{_base(b)}/dashboards/{board_b['id']}/widgets",
            {"title": "steal", "kind": "table", "layout": {"x": 0, "y": 0, "w": 4, "h": 3}, **ref},
            format="json",
        )
        assert resp.status_code in (400, 404), f"{ref} -> {resp.status_code}: {resp.content[:200]}"
    detail = client.get(f"{_base(b)}/dashboards/{board_b['id']}").json()
    assert detail["widgets"] == []


@needs_dashboards
def test_dashboard_viewers_cannot_modify_and_anonymous_gets_401(two_orgs, anon_client):
    a = two_orgs.a
    board = _dashboard(a)
    viewer = api_client_for(a.viewer)
    assert viewer.get(f"{_base(a)}/dashboards/{board['id']}").status_code == 200, "viewer has dashboards.view"
    assert (
        viewer.patch(f"{_base(a)}/dashboards/{board['id']}", {"title": "x"}, format="json").status_code == 403
    )
    assert viewer.delete(f"{_base(a)}/dashboards/{board['id']}").status_code == 403
    assert anon_client.get(f"{_base(a)}/dashboards").status_code == 401
    assert anon_client.get(f"{_base(a)}/dashboards/{board['id']}").status_code == 401
