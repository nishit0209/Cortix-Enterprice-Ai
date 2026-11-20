"""In-app notifications (CONTRACT "W3 additions / Notifications"): `apps.core.notifications.notify()` and the
`/orgs/{org}/notifications` API. Unit tests need no database; `django_db` tests run as nexora_app under RLS."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.db import DatabaseError, connection

from apps.core import notifications
from apps.core.tenancy import TenantContextMissing, tenant_context, tenant_scope
from apps.core.tests.fixtures import add_member, client_for, make_user, member_of, org_url


# ================================================================================================ unit (no DB)
@pytest.mark.parametrize(("link", "expected"), [
    ("", ""),
    (None, ""),
    ("/datasets/0190", "/datasets/0190"),
    ("  /pipelines/1/runs/2  ", "/pipelines/1/runs/2"),
    ("https://evil.example/phish", ""),
    ("//evil.example/x", ""),
    ("javascript:alert(1)", ""),
    ("/ok\r\nSet-Cookie: x=1", ""),
    ("datasets/relative", ""),
])
def test_links_are_app_relative_paths_only(link, expected):
    assert notifications._clean_link(link) == expected


def test_long_links_are_truncated():
    assert notifications._clean_link("/" + "a" * 900) == "/" + "a" * (notifications.LINK_MAX - 1)


def test_notify_needs_a_tenant():
    with pytest.raises(TenantContextMissing):
        notifications.notify([uuid4()], "import.finished", "Import finished")


def test_notify_without_valid_recipients_writes_nothing():
    with tenant_scope(uuid4()):  # no DB access happens: there is nobody to notify
        assert notifications.notify([None, "", "not-a-uuid"], "import.failed", "Import failed") == 0
        assert notifications.notify([], "import.failed", "Import failed") == 0


def test_notify_permission_survives_a_failing_member_lookup(monkeypatch):
    def broken(codename):
        raise DatabaseError("lookup failed")

    monkeypatch.setattr(notifications, "_members_with_permission", [broken])
    with tenant_scope(uuid4()):
        assert notifications.notify_permission("org.settings.edit", "ai.quota_exhausted", "AI quota exhausted") == 0


def test_contract_producer_kinds_are_known():
    assert {"pipeline.run_failed", "pipeline.run_succeeded", "import.finished", "import.failed",
            "document.processed", "invitation.accepted", "ai.quota_exhausted"} <= set(notifications.KINDS)


def test_orgs_registers_the_member_lookup():
    from apps.orgs.services import members_with_permission

    assert notifications._members_with_permission == [members_with_permission]


# ================================================================================================ DB: notify()
@pytest.mark.django_db
def test_notify_creates_one_row_per_distinct_recipient(org, owner):
    from apps.core.models import Notification

    analyst = member_of(org, "analyst")
    with tenant_context(org.id):
        created = notifications.notify([owner.pk, str(owner.pk), analyst.pk, None, "bogus"], "dataset.ingest_finished",
                                       "T" * 300, "Sales is ready: 5,020 rows.", "/datasets/abc")
        assert created == 2
        rows = list(Notification.objects.order_by("user_id"))
        assert sorted(r.user_id for r in rows) == sorted([owner.pk, analyst.pk])
        assert {(r.kind, r.title, r.body, r.link, r.read_at) for r in rows} == {
            ("dataset.ingest_finished", "T" * notifications.TITLE_MAX, "Sales is ready: 5,020 rows.", "/datasets/abc",
             None)}
        assert all(r.org_id == org.id for r in rows)
        assert notifications.notify(owner.pk, "import.finished", "", link="https://evil.example") == 1
        latest = Notification.objects.filter(kind="import.finished").get()
        assert latest.title == "import.finished" and latest.link == ""  # title defaults to the kind; no off-site links


@pytest.mark.django_db
def test_notify_permission_targets_active_members_holding_it(org, owner):
    from apps.core.models import Notification

    admin = member_of(org, "admin")
    member_of(org, "analyst")
    suspended_admin = make_user(full_name="Suspended Admin")
    add_member(org, suspended_admin, "admin", status="suspended")
    with tenant_context(org.id):
        assert notifications.notify_permission("org.settings.edit", "ai.quota_exhausted", "AI quota exhausted",
                                               "Resets at 08:00 UTC.", "/settings/ai") == 2
        assert set(Notification.objects.values_list("user_id", flat=True)) == {owner.pk, admin.pk}


@pytest.mark.django_db
def test_a_storage_failure_never_breaks_the_producer(org, owner, monkeypatch):
    from apps.core.models import Job, Notification

    def boom(*_a, **_k):
        raise DatabaseError("disk full")

    with tenant_context(org.id):
        monkeypatch.setattr(Notification.objects, "bulk_create", boom)
        assert notifications.notify([owner.pk], "import.failed", "Import failed") == 0
        Job.objects.create(type="unit.after_notify", user_id=owner.pk)  # the producer's transaction still works
        assert Job.objects.filter(type="unit.after_notify").count() == 1


@pytest.mark.django_db
def test_notifications_are_tenant_isolated(org, other_org, owner):
    from apps.core.models import Notification

    with tenant_context(org.id):
        notifications.notify([owner.pk], "document.processed", "Contract.pdf processed")
    with tenant_context(other_org.id):
        assert Notification.objects.count() == 0
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM core_notification")
            assert cur.fetchone()[0] == 0


# ================================================================================================ DB: API
def _seed(org, user, n: int, kind: str = "import.finished") -> list[str]:
    from apps.core.models import Notification

    with tenant_context(org.id):
        for i in range(n):
            notifications.notify([user.pk], kind, f"{kind} #{i}", link=f"/imports/{i}")
        return [str(i) for i in Notification.objects.filter(user_id=user.pk).order_by("-created_at", "-id")
                .values_list("id", flat=True)]


@pytest.mark.django_db
def test_list_is_own_newest_first_with_unread_count(org, owner, owner_client):
    analyst = member_of(org, "analyst")
    ids = _seed(org, owner, 3)
    _seed(org, analyst, 2, kind="import.failed")
    r = owner_client.get(org_url(org, "notifications"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "next_cursor", "previous_cursor", "unread_count"}
    assert [i["id"] for i in body["items"]] == ids and body["unread_count"] == 3
    assert set(body["items"][0]) == {"id", "kind", "title", "body", "link", "read_at", "created_at"}
    assert {i["kind"] for i in body["items"]} == {"import.finished"}  # never another user's notifications
    page = owner_client.get(org_url(org, "notifications?limit=2")).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = owner_client.get(page["next_cursor"]).json()
    assert [i["id"] for i in rest["items"]] == ids[2:]


@pytest.mark.django_db
def test_unread_filter_and_mark_read(org, owner, owner_client):
    from apps.core.models import AuditEvent

    ids = _seed(org, owner, 3)
    r = owner_client.post(org_url(org, f"notifications/{ids[0]}/read"))
    assert r.status_code == 200
    assert r.json()["id"] == ids[0] and r.json()["read_at"] and r.json()["unread_count"] == 2
    again = owner_client.post(org_url(org, f"notifications/{ids[0]}/read"))
    assert again.status_code == 200 and again.json()["read_at"] == r.json()["read_at"]  # idempotent
    unread = owner_client.get(org_url(org, "notifications?unread=1")).json()
    assert [i["id"] for i in unread["items"]] == ids[1:] and unread["unread_count"] == 2
    read = owner_client.get(org_url(org, "notifications?unread=0")).json()
    assert [i["id"] for i in read["items"]] == ids[:1]
    assert owner_client.get(org_url(org, "notifications?unread=maybe")).status_code == 400
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="notification.read").count() == 2


@pytest.mark.django_db
def test_read_all_marks_only_the_callers_notifications(org, owner, owner_client):
    analyst = member_of(org, "analyst")
    _seed(org, owner, 2)
    _seed(org, analyst, 1)
    r = owner_client.post(org_url(org, "notifications/read-all"))
    assert r.status_code == 200 and r.json() == {"updated": 2, "unread_count": 0}
    assert owner_client.post(org_url(org, "notifications/read-all")).json() == {"updated": 0, "unread_count": 0}
    assert client_for(analyst).get(org_url(org, "notifications?unread=1")).json()["unread_count"] == 1


@pytest.mark.django_db
def test_other_users_and_other_orgs_get_404(org, other_org, owner, owner_client):
    analyst = member_of(org, "analyst")
    (owner_note,) = _seed(org, owner, 1)
    analyst_client = client_for(analyst)
    assert analyst_client.post(org_url(org, f"notifications/{owner_note}/read")).status_code == 404  # cross-user
    assert analyst_client.post(org_url(org, f"notifications/{uuid4()}/read")).status_code == 404
    # the owner joins the other org too: org A's notification is invisible through org B's slug (RLS)
    add_member(other_org, owner, "viewer")
    assert owner_client.post(org_url(other_org, f"notifications/{owner_note}/read")).status_code == 404
    assert owner_client.get(org_url(other_org, "notifications")).json()["items"] == []
    with tenant_context(other_org.id):
        from apps.orgs.models import Membership

        stranger = Membership.objects.exclude(user=owner).get().user
    stranger_client = client_for(stranger)
    assert stranger_client.get(org_url(org, "notifications")).status_code == 404  # not a member of org
    assert stranger_client.post(org_url(org, f"notifications/{owner_note}/read")).status_code == 404
    assert client_for(None).get(org_url(org, "notifications")).status_code == 401
    with tenant_context(org.id):
        from apps.core.models import Notification

        assert Notification.objects.get(id=owner_note).read_at is None  # nobody else could mark it read
