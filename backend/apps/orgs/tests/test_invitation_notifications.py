"""Notification producer (CONTRACT "W3 additions / Notifications"): accepting an invitation notifies the inviter
and the members who manage members, never the person who joined."""

import re

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import Notification
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, client_for, make_user, member_of, org_url

pytestmark = pytest.mark.django_db

ACCEPT = "/api/v1/invitations/accept"
NEW_PASSWORD = "Fresh-Synthetic-Pass-42!"


def _token(mail) -> str:
    m = re.search(r"token=([A-Za-z0-9_\-]+)", mail.body)
    assert m, mail.body
    return m.group(1)


def test_accepting_an_invitation_notifies_inviter_and_member_managers(org, owner, owner_client, mailoutbox):
    admin = member_of(org, "admin")
    analyst = member_of(org, "analyst")
    r = owner_client.post(org_url(org, "members"), {"email": "joiner@example.test", "role": "viewer"}, format="json")
    assert r.status_code in (200, 201), r.content
    joined = APIClient().post(ACCEPT, {"token": _token(mailoutbox[0]), "password": NEW_PASSWORD,
                                       "full_name": "Jo Joiner"}, format="json")
    assert joined.status_code == 200, joined.content
    joiner = User.objects.get(email="joiner@example.test")
    with tenant_context(org.id):
        rows = list(Notification.objects.filter(kind="invitation.accepted"))
        assert {n.user_id for n in rows} == {owner.pk, admin.pk}  # not the analyst, not the joiner
        assert {(n.title, n.link) for n in rows} == {("Jo Joiner joined the organization", "/settings/members")}
        assert all("joiner@example.test" in n.body and "Viewer" in n.body for n in rows)
        assert not Notification.objects.filter(user_id__in=[joiner.pk, analyst.pk]).exists()
    items = owner_client.get(org_url(org, "notifications?unread=1")).json()["items"]
    assert [i["kind"] for i in items] == ["invitation.accepted"]


def test_an_existing_user_joining_notifies_once_per_recipient(org, owner, owner_client, mailoutbox):
    """The inviter also holds members.manage: one notification, not two."""
    existing = make_user(email="existing.joiner@example.test", full_name="Ex Isting")
    r = owner_client.post(org_url(org, "members"), {"email": existing.email, "role": "analyst"}, format="json")
    assert r.status_code in (200, 201), r.content
    joined = APIClient().post(ACCEPT, {"token": _token(mailoutbox[0]), "password": DEFAULT_PASSWORD}, format="json")
    assert joined.status_code == 200, joined.content
    with tenant_context(org.id):
        assert list(Notification.objects.values_list("user_id", flat=True)) == [owner.pk]
    assert client_for(existing).get(org_url(org, "notifications")).json()["items"] == []
