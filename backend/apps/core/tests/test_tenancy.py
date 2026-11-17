"""Wave-0 sanity tests for the tenancy core. Runs as nexora_app (RLS enforced)."""

import pytest
from django.db import connection

from apps.accounts.models import User
from apps.core.tenancy import TenantContextMissing, tenant_context
from apps.core.tenancy.views import resolve_membership
from apps.orgs.models import Membership, Organization, Role
from apps.orgs.services import create_organization, user_orgs

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_orgs():
    alice = User.objects.create_user("alice@example.test", "pw-Alice-123456")
    bob = User.objects.create_user("bob@example.test", "pw-Bob-1234567")
    org_a = create_organization(name="Acme Analytics", owner=alice)
    org_b = create_organization(name="Beta Retail", owner=bob)
    return alice, bob, org_a, org_b


def test_runs_as_app_role_without_bypassrls():
    with connection.cursor() as cur:
        cur.execute("SELECT current_user, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        user, bypass = cur.fetchone()
    assert user == "nexora_app"
    assert bypass is False


@pytest.mark.ac("P2-AC3")
def test_new_org_defaults_to_synthetic_only(two_orgs):
    _, _, org_a, _ = two_orgs
    with tenant_context(org_a.id):
        assert Organization.objects.get().ai_data_policy == "synthetic_only"


@pytest.mark.ac("P2-AC2")
def test_no_context_sees_zero_rows_and_manager_fails_closed(two_orgs):
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM orgs_membership")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM orgs_organization")
        assert cur.fetchone()[0] == 0
    with pytest.raises(TenantContextMissing):
        list(Role.objects.all())


@pytest.mark.ac("P2-AC2")
def test_rls_isolates_orgs(two_orgs):
    alice, bob, org_a, org_b = two_orgs
    with tenant_context(org_a.id):
        assert set(Membership.objects.values_list("user_id", flat=True)) == {alice.id}
        with connection.cursor() as cur:
            cur.execute("SELECT count(DISTINCT org_id) FROM orgs_role")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM orgs_role WHERE org_id = %s", [str(org_b.id)])
            assert cur.fetchone()[0] == 0


@pytest.mark.ac("P2-AC2")
def test_cross_org_insert_violates_with_check(two_orgs):
    from django.db import DatabaseError, transaction

    _, bob, org_a, org_b = two_orgs
    with tenant_context(org_a.id):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "INSERT INTO orgs_role (id, org_id, key, name, description, is_system, created_at, updated_at) "
                "VALUES (gen_random_uuid(), %s, 'evil', 'Evil', '', false, now(), now())",
                [str(org_b.id)],
            )


def test_membership_lookup_and_user_orgs(two_orgs):
    alice, bob, org_a, org_b = two_orgs
    info = resolve_membership(alice.id, org_a.slug)
    assert info is not None and info.role_key == "owner" and info.has_perm("org.ai_policy.consent")
    assert resolve_membership(alice.id, org_b.slug) is None
    assert [o["slug"] for o in user_orgs(alice.id)] == [org_a.slug]


def test_audit_event_is_append_only(two_orgs):
    from django.db import DatabaseError, transaction

    from apps.core.models import AuditEvent

    _, _, org_a, _ = two_orgs
    with tenant_context(org_a.id):
        ev = AuditEvent.objects.filter(action="org.create").first()
        assert ev is not None
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditEvent.objects.filter(id=ev.id).update(action="tampered")
