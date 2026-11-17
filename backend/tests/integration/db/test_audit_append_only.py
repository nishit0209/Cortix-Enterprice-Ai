"""Audit trail: append-only at the database (trigger + revoked grants), redaction, tenant scoping, API access."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, connection, transaction

from apps.core import audit
from apps.core.tenancy import TenantContextMissing
from testing.client import api_client_for
from testing.db import sqlstate

pytestmark = pytest.mark.django_db


@pytest.fixture
def event(two_orgs, tenant):
    with tenant(two_orgs.a.org):
        return audit.record("qa.audit.probe", target_type="qa.thing", target_id="t-1", metadata={"n": 1})


def _expect_denied(sql: str, params=None) -> str:
    with pytest.raises(DatabaseError) as info, transaction.atomic(), connection.cursor() as cur:
        cur.execute(sql, params or [])
    return sqlstate(info.value)


def test_update_and_delete_are_refused_by_the_database(two_orgs, tenant, event):
    from apps.core.models import AuditEvent

    with tenant(two_orgs.a.org):
        assert (
            _expect_denied("UPDATE core_auditevent SET action = 'tampered' WHERE id = %s", [str(event.id)])
            == "42501"
        )
        assert _expect_denied("DELETE FROM core_auditevent WHERE id = %s", [str(event.id)]) == "42501"
        assert _expect_denied("TRUNCATE core_auditevent") == "42501"
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(id=event.id).update(action="tampered")
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(id=event.id).delete()
        assert AuditEvent.objects.get(id=event.id).action == "qa.audit.probe"


def test_app_role_lacks_update_delete_privileges():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege(current_user, 'core_auditevent', 'UPDATE'), "
            "has_table_privilege(current_user, 'core_auditevent', 'DELETE'), "
            "has_table_privilege(current_user, 'core_auditevent', 'TRUNCATE'), "
            "has_table_privilege(current_user, 'core_auditevent', 'INSERT')"
        )
        assert cur.fetchone() == (False, False, False, True)


def test_append_only_trigger_exists():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'core_auditevent'::regclass AND NOT tgisinternal"
        )
        assert "append_only" in {r[0] for r in cur.fetchall()}


def test_record_requires_tenant_context(db):
    with pytest.raises(TenantContextMissing):
        audit.record("qa.no_ctx")


def test_record_redacts_secret_keys_recursively(two_orgs, tenant):
    with tenant(two_orgs.a.org):
        ev = audit.record(
            "qa.redact",
            metadata={
                "password": "pw-1",
                "API_KEY": "k-1",
                "nested": {"refresh_token": "t-1", "keep": "ok"},
                "items": [{"Authorization": "Bearer x"}, {"cookie": "c"}],
                "client_secret": "s",
                "db_credential": "c-1",
            },
            changes={"password": ["old", "new"], "name": ["a", "b"]},
        )
    md = ev.metadata
    assert md["password"] == md["API_KEY"] == md["client_secret"] == md["db_credential"] == "***"
    assert md["nested"] == {"refresh_token": "***", "keep": "ok"}
    assert md["items"] == [{"Authorization": "***"}, {"cookie": "***"}]
    assert ev.changes == {"password": "***", "name": ["a", "b"]}


def test_record_attributes_actor_and_target(two_orgs, tenant):
    a = two_orgs.a
    with tenant(a.org):
        ev = audit.record("qa.actor", target=a.org, actor=("user", a.owner.pk))
    assert (ev.actor_type, ev.actor_id) == ("user", a.owner.pk)
    assert ev.target_type == "orgs.organization" and ev.target_id == str(a.id)
    assert ev.org_id == a.id


def test_org_creation_is_audited(two_orgs, tenant):
    from apps.core.models import AuditEvent

    a = two_orgs.a
    with tenant(a.org):
        ev = AuditEvent.objects.get(action="org.create")
    assert ev.actor_id == a.owner.pk and ev.metadata["slug"] == a.slug


def test_audit_api_requires_audit_view_and_is_org_scoped(two_orgs, tenant, event):
    a, b = two_orgs
    with tenant(b.org):
        audit.record("qa.audit.b-only")
    url = f"/api/v1/orgs/{a.slug}/audit-events"
    assert api_client_for(a.analyst).get(url).status_code == 403  # analyst lacks audit.view
    resp = api_client_for(a.user("auditor")).get(url)
    assert resp.status_code == 200
    actions = {i["action"] for i in resp.json()["items"]}
    assert "qa.audit.probe" in actions and "qa.audit.b-only" not in actions
    filtered = api_client_for(a.owner).get(url, {"action": "qa.audit.probe"}).json()["items"]
    assert [i["action"] for i in filtered] == ["qa.audit.probe"]
