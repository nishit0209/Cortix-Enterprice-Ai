"""Members (list/update/remove, last-owner protection) and invitations (invite/revoke/accept)."""

import hashlib
import re
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, add_member, client_for, make_user, member_of, org_url
from apps.orgs.models import Invitation, Membership, Role

pytestmark = pytest.mark.django_db

ACCEPT = "/api/v1/invitations/accept"
NEW_PASSWORD = "Fresh-Synthetic-Pass-42!"


def membership_id(org, user) -> str:
    with tenant_context(org.id):
        return str(Membership.objects.get(user=user).id)


def token_from(mail) -> str:
    m = re.search(r"token=([A-Za-z0-9_\-]+)", mail.body)
    assert m, mail.body
    return m.group(1)


def invite(client, org, email, role="analyst"):
    return client.post(org_url(org, "members"), {"email": email, "role": role}, format="json")


# --- members list --------------------------------------------------------------------------------------------------
def test_members_list_shape_and_permissions(org, owner, owner_client, other_org):
    analyst = member_of(org, "analyst", full_name="Ana Analyst")
    r = owner_client.get(org_url(org, "members"))
    assert r.status_code == 200
    items = r.json()["items"]
    by_email = {m["user"]["email"]: m for m in items}
    assert set(by_email) == {owner.email, analyst.email}
    m = by_email[analyst.email]
    assert {"id", "user", "role", "status", "joined_at"} <= set(m)
    assert m["user"] == {"id": str(analyst.id), "email": analyst.email, "full_name": "Ana Analyst"}
    assert m["role"] == "analyst" and m["status"] == "active"
    assert client_for(analyst).get(org_url(org, "members")).status_code == 200  # members.view
    assert client_for(member_of(org, "viewer")).get(org_url(org, "members")).status_code == 403
    assert owner_client.get(org_url(other_org, "members")).status_code == 404


# --- invite ---------------------------------------------------------------------------------------------------------
def test_invite_sends_email_and_stores_only_hash(org, owner_client, mailoutbox):
    r = invite(owner_client, org, "New.Person@Example.test")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["email"] == "new.person@example.test"
    assert body["role"] == "analyst" and body["status"] == "pending" and body["email_sent"] is True
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["new.person@example.test"]
    token = token_from(mailoutbox[0])
    with tenant_context(org.id):
        inv = Invitation.objects.get(id=body["id"])
        assert bytes(inv.token_hash) == hashlib.sha256(token.encode()).digest()
        assert (inv.expires_at - timezone.now()) > timedelta(days=6, hours=23)
        assert AuditEvent.objects.filter(action="invitation.create").exists()
    listed = owner_client.get(org_url(org, "invitations")).json()["items"]
    assert [i["id"] for i in listed] == [body["id"]]


def test_invite_validation_and_conflicts(org, owner_client):
    analyst = member_of(org, "analyst")
    r = invite(owner_client, org, analyst.email.upper())
    assert r.status_code == 409 and r.json()["code"] == "already_member"
    assert invite(owner_client, org, "x@example.test", role="emperor").status_code == 400
    assert invite(owner_client, org, "not-an-email").status_code == 400


def test_invite_permissions(org, other_org, owner_client):
    analyst = client_for(member_of(org, "analyst"))
    assert invite(analyst, org, "a@example.test").status_code == 403
    admin = client_for(member_of(org, "admin"))
    r = invite(admin, org, "boss@example.test", role="owner")
    assert r.status_code == 403 and r.json()["code"] == "owner_required"
    assert invite(admin, org, "helper@example.test", role="admin").status_code == 201
    assert invite(owner_client, org, "owner2@example.test", role="owner").status_code == 201
    assert invite(owner_client, other_org, "x@example.test").status_code == 404


def test_reinvite_refreshes_token(org, owner_client, mailoutbox):
    first = invite(owner_client, org, "again@example.test").json()
    second = invite(owner_client, org, "again@example.test", role="viewer").json()
    assert first["id"] == second["id"] and second["role"] == "viewer"
    old_token, new_token = token_from(mailoutbox[0]), token_from(mailoutbox[1])
    anon = APIClient()
    assert anon.post(ACCEPT, {"token": old_token, "password": NEW_PASSWORD}, format="json").status_code == 400
    assert anon.post(ACCEPT, {"token": new_token, "password": NEW_PASSWORD}, format="json").status_code == 200


def test_invitations_endpoint_post_and_revoke(org, other_org, owner_client, mailoutbox):
    r = owner_client.post(org_url(org, "invitations"), {"email": "rev@example.test", "role": "viewer"}, format="json")
    assert r.status_code == 201
    inv_id = r.json()["id"]
    token = token_from(mailoutbox[0])
    with tenant_context(other_org.id):
        foreign = Invitation.objects.create(
            email="f@example.test", role=Role.objects.get(key="viewer"),
            token_hash=b"x" * 32, expires_at=timezone.now() + timedelta(days=1), invited_by=other_org.id)
    assert owner_client.delete(org_url(org, f"invitations/{foreign.id}")).status_code == 404
    assert owner_client.delete(org_url(org, f"invitations/{inv_id}")).status_code == 204
    assert owner_client.get(org_url(org, "invitations")).json()["items"] == []
    all_items = owner_client.get(org_url(org, "invitations") + "?status=all").json()["items"]
    assert all_items[0]["status"] == "revoked"
    r = APIClient().post(ACCEPT, {"token": token, "password": NEW_PASSWORD}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "invitation_invalid"
    assert client_for(member_of(org, "analyst")).get(org_url(org, "invitations")).status_code == 403


# --- accept ---------------------------------------------------------------------------------------------------------
def test_accept_as_new_user_creates_account_and_logs_in(org, owner_client, mailoutbox):
    invite(owner_client, org, "brand.new@example.test", role="data_engineer")
    client = APIClient()
    r = client.post(ACCEPT, {"token": token_from(mailoutbox[0]), "password": NEW_PASSWORD, "full_name": "Bree New"},
                    format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["org"]["slug"] == org.slug and body["org"]["role"] == "data_engineer"
    assert "pipelines.manage" in body["org"]["permissions"]
    user = User.objects.get(email="brand.new@example.test")
    assert user.full_name == "Bree New" and user.check_password(NEW_PASSWORD)
    me = client.get("/api/v1/me").json()
    assert [o["slug"] for o in me["orgs"]] == [org.slug]
    with tenant_context(org.id):
        assert Invitation.objects.get().status == "accepted"
        assert AuditEvent.objects.filter(action="invitation.accept").exists()
    again = APIClient().post(ACCEPT, {"token": token_from(mailoutbox[0]), "password": NEW_PASSWORD}, format="json")
    assert again.status_code == 400


def test_accept_new_user_password_rules(org, owner_client, mailoutbox):
    invite(owner_client, org, "weak@example.test")
    token = token_from(mailoutbox[0])
    assert APIClient().post(ACCEPT, {"token": token}, format="json").status_code == 400
    r = APIClient().post(ACCEPT, {"token": token, "password": "123"}, format="json")
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "password"


def test_accept_existing_user_requires_their_password(org, owner_client, mailoutbox):
    existing = make_user(email="existing@example.test")
    invite(owner_client, org, existing.email, role="viewer")
    token = token_from(mailoutbox[0])
    bad = APIClient().post(ACCEPT, {"token": token, "password": "Wrong-password-xyz"}, format="json")
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_credentials"
    ok = APIClient().post(ACCEPT, {"token": token, "password": DEFAULT_PASSWORD}, format="json")
    assert ok.status_code == 200 and ok.json()["org"]["role"] == "viewer"


def test_accept_logged_in_user_email_must_match(org, owner_client, mailoutbox):
    invited = make_user(email="invited@example.test")
    invite(owner_client, org, invited.email)
    token = token_from(mailoutbox[0])
    stranger = client_for(make_user(email="stranger@example.test"))
    r = stranger.post(ACCEPT, {"token": token}, format="json")
    assert r.status_code == 403 and r.json()["code"] == "invitation_email_mismatch"
    r = client_for(invited).post(ACCEPT, {"token": token}, format="json")
    assert r.status_code == 200 and r.json()["org"]["role"] == "analyst"


def test_accept_expired_or_bogus_token(org, owner_client, mailoutbox):
    invite(owner_client, org, "late@example.test")
    token = token_from(mailoutbox[0])
    with tenant_context(org.id):
        Invitation.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    r = APIClient().post(ACCEPT, {"token": token, "password": NEW_PASSWORD}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "invitation_invalid"
    assert APIClient().post(ACCEPT, {"token": "bogus", "password": NEW_PASSWORD}, format="json").status_code == 400
    assert APIClient().post(ACCEPT, {}, format="json").status_code == 400
    listed = owner_client.get(org_url(org, "invitations")).json()["items"]
    assert listed[0]["status"] == "expired"


def test_accept_enforces_csrf(org, owner_client, mailoutbox):
    invite(owner_client, org, "csrf@example.test")
    client = APIClient(enforce_csrf_checks=True)
    r = client.post(ACCEPT, {"token": token_from(mailoutbox[0]), "password": NEW_PASSWORD}, format="json")
    assert r.status_code == 403


# --- update / remove ---------------------------------------------------------------------------------------------------
def test_change_role(org, owner_client):
    analyst = member_of(org, "analyst")
    mid = membership_id(org, analyst)
    r = owner_client.patch(org_url(org, f"members/{mid}"), {"role": "data_engineer"}, format="json")
    assert r.status_code == 200 and r.json()["role"] == "data_engineer"
    with tenant_context(org.id):
        ev = AuditEvent.objects.get(action="member.update")
        assert ev.changes == {"role": ["analyst", "data_engineer"]}
    assert owner_client.patch(org_url(org, f"members/{mid}"), {"role": "emperor"}, format="json").status_code == 400
    assert owner_client.patch(org_url(org, f"members/{mid}"), {}, format="json").status_code == 400
    r = owner_client.patch(org_url(org, f"members/{mid}"), {"status": "suspended"}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "suspended"


def test_last_owner_cannot_be_demoted_suspended_or_removed(org, owner, owner_client):
    mid = membership_id(org, owner)
    r = owner_client.patch(org_url(org, f"members/{mid}"), {"role": "admin"}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "last_owner"
    assert owner_client.patch(org_url(org, f"members/{mid}"), {"status": "suspended"},
                              format="json").status_code == 409
    assert owner_client.delete(org_url(org, f"members/{mid}")).status_code == 409
    second = member_of(org, "owner")
    assert owner_client.patch(org_url(org, f"members/{mid}"), {"role": "admin"}, format="json").status_code == 200
    # now `second` is the last owner
    r = client_for(second).delete(org_url(org, f"members/{membership_id(org, second)}"))
    assert r.status_code == 409


def test_only_owners_touch_owner_role(org, owner):
    admin = client_for(member_of(org, "admin"))
    owner_mid = membership_id(org, owner)
    r = admin.patch(org_url(org, f"members/{owner_mid}"), {"role": "viewer"}, format="json")
    assert r.status_code == 403 and r.json()["code"] == "owner_required"
    assert admin.delete(org_url(org, f"members/{owner_mid}")).status_code == 403
    analyst = member_of(org, "analyst")
    r = admin.patch(org_url(org, f"members/{membership_id(org, analyst)}"), {"role": "owner"}, format="json")
    assert r.status_code == 403


def test_remove_member(org, other_org, owner_client):
    analyst = member_of(org, "analyst")
    mid = membership_id(org, analyst)
    with tenant_context(other_org.id):
        foreign_mid = str(Membership.objects.get().id)
    assert owner_client.delete(org_url(org, f"members/{foreign_mid}")).status_code == 404
    assert owner_client.patch(org_url(org, f"members/{foreign_mid}"), {"role": "viewer"},
                              format="json").status_code == 404
    assert client_for(analyst).delete(org_url(org, f"members/{mid}")).status_code == 403
    assert owner_client.delete(org_url(org, f"members/{mid}")).status_code == 204
    assert client_for(analyst).get(org_url(org)).status_code == 404
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="member.remove").exists()


def test_member_routes_reject_non_uuid(org, owner_client):
    assert owner_client.delete(org_url(org, "members/not-a-uuid")).status_code == 404


def test_add_member_helper_respects_rls(org, other_org):
    user = make_user()
    add_member(org, user, "viewer")
    with tenant_context(other_org.id):
        assert not Membership.objects.filter(user=user).exists()
