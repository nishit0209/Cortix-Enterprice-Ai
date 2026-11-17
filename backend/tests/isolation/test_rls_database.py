"""Database-level tenant isolation (MULTI_TENANCY_DESIGN §7.2): every tenant table, discovered dynamically.

For EVERY public table with an `org_id` column (plus orgs_organization via `id`):
  * RLS is enabled AND forced, with a `tenant_isolation` policy whose USING and WITH CHECK use the tenant setting;
  * with no tenant context, zero rows are visible;
  * under org B's context, no row of another org is visible;
  * INSERTing a row for org A under org B's context fails the WITH CHECK (42501);
  * moving one of B's rows to org A with UPDATE fails.
Every TenantModel subclass's table must be covered (new models from other agents are picked up automatically).
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.apps import apps as django_apps
from django.db import DatabaseError, connection, transaction

from apps.core.tenancy.constants import TENANT_GUC
from apps.core.tenancy.models import TenantModel
from testing.db import clear_db_tenant, db_tenant, rls_status, sqlstate, table_columns, tenant_tables
from testing.factories import make_job

pytestmark = pytest.mark.django_db


@pytest.fixture
def populated(two_orgs, tenant):
    """Two orgs with rows in as many tenant tables as the wave has (core + orgs)."""
    from apps.core import audit

    a, b = two_orgs
    for world in (a, b):
        make_job(world.org, user=world.owner, type="qa.rls")
        with tenant(world.org):
            audit.record("qa.rls.probe", metadata={"probe": True})
    clear_db_tenant()
    return two_orgs


def _count(table: str, where: str = "", params: list | None = None) -> int:
    with connection.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{table}" {where}', params or [])
        return cur.fetchone()[0]


def test_tenant_tables_are_discovered():
    tables = tenant_tables()
    for expected in ("orgs_organization", "orgs_membership", "orgs_role", "orgs_rolepermission",
                     "core_job", "core_auditevent"):
        assert expected in tables


def test_every_tenant_table_has_forced_rls_and_the_canonical_policy():
    problems = []
    for table, column in tenant_tables().items():
        st = rls_status(table)
        if not st.enabled:
            problems.append(f"{table}: RLS not enabled")
        if not st.forced:
            problems.append(f"{table}: RLS not FORCED (owner would bypass)")
        pol = st.tenant_policy
        if pol is None:
            problems.append(f"{table}: no tenant_isolation policy")
            continue
        using, check = pol
        for label, expr in (("USING", using), ("WITH CHECK", check)):
            if not expr or TENANT_GUC not in expr or column not in expr or "current_setting" not in expr:
                problems.append(f"{table}: {label} clause is not the canonical tenant predicate: {expr!r}")
        # Any additional permissive policy would OR with tenant_isolation and could widen access.
        extra = sorted(set(st.policies) - {"tenant_isolation"})
        if extra:
            problems.append(f"{table}: extra policies {extra} must be reviewed (permissive policies OR together)")
    assert problems == []


def test_every_tenant_model_table_has_rls():
    tables = tenant_tables()
    missing = []
    for model in django_apps.get_models():
        if issubclass(model, TenantModel):
            t = model._meta.db_table
            st = rls_status(t)
            if t not in tables or not (st.enabled and st.forced and st.tenant_policy):
                missing.append(t)
    assert missing == []


def test_zero_rows_visible_without_tenant_context(populated):
    clear_db_tenant()
    visible = {t: _count(t) for t in tenant_tables()}
    assert {t: n for t, n in visible.items() if n} == {}


def test_unset_and_empty_setting_both_match_nothing(populated):
    with db_tenant(None):
        assert _count("orgs_membership") == 0
    with db_tenant(str(uuid.uuid4())):  # an org that does not exist
        assert _count("orgs_membership") == 0


def test_no_cross_org_rows_under_other_orgs_context(populated):
    a, b = populated
    leaks = {}
    with db_tenant(b.id):
        for table, column in tenant_tables().items():
            foreign = _count(table, f'WHERE "{column}" <> %s', [str(b.id)])
            a_rows = _count(table, f'WHERE "{column}" = %s', [str(a.id)])
            if foreign or a_rows:
                leaks[table] = (foreign, a_rows)
        assert _count("orgs_membership") >= 1  # B's own rows ARE visible (the probe is meaningful)
    assert leaks == {}


def _clone_row_sql(table: str) -> str:
    # Copy one visible row, overriding org/id via jsonb: works for any column set without knowing it.
    return (
        f'INSERT INTO "{table}" SELECT (jsonb_populate_record(NULL::"{table}", to_jsonb(x) || %s::jsonb)).* '
        f'FROM "{table}" x LIMIT 1'
    )


def test_cross_org_insert_fails_with_check(populated):
    a, b = populated
    checked, failures = [], []
    with db_tenant(b.id):
        for table, column in tenant_tables().items():
            if _count(table) == 0:
                continue  # nothing to clone in this wave; covered once the owning app has data
            cols = table_columns(table)
            override = {column: str(a.id)}
            if column != "id" and cols.get("id") == "uuid":
                override["id"] = str(uuid.uuid4())
            elif column == "id":
                override["slug"] = f"x-{uuid.uuid4().hex[:8]}"
                override["short_id"] = uuid.uuid4().hex[:8]
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(_clone_row_sql(table), [json.dumps(override)])
                failures.append(f"{table}: cross-org INSERT succeeded")
            except DatabaseError as exc:
                if sqlstate(exc) != "42501":
                    failures.append(f"{table}: expected RLS violation 42501, got {sqlstate(exc)}: {exc}")
            checked.append(table)
    assert {"orgs_role", "orgs_membership", "core_job", "core_auditevent", "orgs_organization"} <= set(checked)
    assert failures == []


def test_moving_a_row_to_another_org_fails(populated):
    a, b = populated
    failures = []
    with db_tenant(b.id):
        for table, column in tenant_tables().items():
            if column == "id" or _count(table) == 0:
                continue
            try:
                with transaction.atomic(), connection.cursor() as cur:
                    cur.execute(
                        f'UPDATE "{table}" SET "{column}" = %s WHERE ctid = (SELECT ctid FROM "{table}" LIMIT 1)',
                        [str(a.id)],
                    )
                    if cur.rowcount:
                        failures.append(f"{table}: row moved to another org")
            except DatabaseError as exc:
                if sqlstate(exc) != "42501":
                    failures.append(f"{table}: expected 42501, got {sqlstate(exc)}")
    assert failures == []


def test_rows_of_other_org_cannot_be_deleted_or_updated(populated):
    a, b = populated
    with db_tenant(a.id):
        with connection.cursor() as cur:
            cur.execute("SELECT id FROM core_job")
            a_job = cur.fetchone()[0]
    with db_tenant(b.id), connection.cursor() as cur:
        cur.execute("UPDATE core_job SET status = 'cancelled' WHERE id = %s", [a_job])
        assert cur.rowcount == 0
        cur.execute("DELETE FROM core_job WHERE id = %s", [a_job])
        assert cur.rowcount == 0
    with db_tenant(a.id), connection.cursor() as cur:
        cur.execute("SELECT status FROM core_job WHERE id = %s", [a_job])
        assert cur.fetchone()[0] == "queued"


def test_app_role_cannot_disable_rls_or_bypass(db):
    with connection.cursor() as cur:
        cur.execute("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")
        assert cur.fetchone() == (False, False)
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute('ALTER TABLE "core_job" DISABLE ROW LEVEL SECURITY')
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute('DROP POLICY tenant_isolation ON "core_job"')
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("SET ROLE nexora_ops")


def test_security_definer_lookups_do_not_leak_other_orgs(populated):
    """nexora_membership_check / nexora_user_orgs bypass RLS by design; they must only answer for the caller."""
    from apps.core.tenancy.views import resolve_membership
    from apps.orgs.services import user_orgs

    a, b = populated
    assert resolve_membership(a.owner.pk, b.slug) is None
    assert resolve_membership(b.analyst.pk, a.slug) is None
    assert resolve_membership(populated.outsider.pk, a.slug) is None
    assert [o["slug"] for o in user_orgs(a.analyst.pk)] == [a.slug]
    assert user_orgs(populated.outsider.pk) == []
    info = resolve_membership(a.viewer.pk, a.slug)
    assert info is not None and info.role_key == "viewer" and not info.has_perm("members.manage")


def test_suspended_membership_and_inactive_user_lose_access(populated, tenant):
    from apps.core.tenancy.views import resolve_membership
    from apps.orgs.models import Membership

    a = populated.a
    with tenant(a.org):
        Membership.objects.filter(user=a.analyst).update(status="suspended")
    assert resolve_membership(a.analyst.pk, a.slug) is None
    a.viewer.is_active = False
    a.viewer.save(update_fields=["is_active"])
    assert resolve_membership(a.viewer.pk, a.slug) is None
