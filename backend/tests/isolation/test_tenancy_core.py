"""Verification of apps.core.tenancy (context, fail-closed ORM, RLS second layer, TenantTask, GUC lifetime).

Runs as nexora_app (RLS enforced). Tests named in a "Regression for QA defect ..." docstring reproduce W1
defects; they are plain tests now and fail until the Backend W2 fix is merged.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from celery import shared_task
from django.apps import apps as django_apps
from django.db import DatabaseError, connection, transaction

from apps.core.tenancy import TenantContextMissing, current_org, tenant_context
from apps.core.tenancy.constants import TENANT_GUC
from apps.core.tenancy.context import TenantMismatch, current_actor, current_tenant
from apps.core.tenancy.models import TenantManager, TenantModel
from apps.core.tenancy.tasks import OrgInactive, TenantTask, tenant_step
from testing.db import clear_db_tenant, db_tenant_value, independent_connection
from testing.factories import make_job, set_org_fields

BACKEND = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------------- probe tasks
@shared_task(base=TenantTask, name="qa.tenancy.probe")
def probe_task(*, org_id, actor_id=None):
    from apps.core.models import Job

    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT org_id::text FROM core_job")
        raw_orgs = sorted(r[0] for r in cur.fetchall())
    return {
        "org": str(current_org.get()),
        "guc": db_tenant_value(),
        "jobs": sorted(str(i) for i in Job.objects.values_list("id", flat=True)),
        "raw_orgs": raw_orgs,
        "actor": current_actor.get(),
    }


@shared_task(base=TenantTask, name="qa.tenancy.long_probe", long_running=True)
def long_probe_task(*, org_id):
    outside = current_org.get()
    with tenant_step(org_id):
        inside = current_org.get()
        guc = db_tenant_value()
    return {"outside": outside, "inside": str(inside), "guc": guc}


def _tenant_models():
    return [m for m in django_apps.get_models() if issubclass(m, TenantModel)]


# ----------------------------------------------------------------------------------- context
def test_no_context_by_default():
    assert current_org.get() is None
    assert current_tenant.get() is None


def test_context_sets_contextvar_and_transaction_local_guc(two_orgs):
    a = two_orgs.a
    with tenant_context(a.id):
        assert current_org.get() == a.id
        assert current_tenant.get().org_id == a.id
        assert db_tenant_value() == str(a.id)
    assert current_org.get() is None
    assert current_tenant.get() is None


def test_context_accepts_string_ids(two_orgs):
    with tenant_context(str(two_orgs.a.id)):
        assert current_org.get() == two_orgs.a.id


def test_nested_same_org_is_allowed_and_restores_outer(two_orgs):
    a = two_orgs.a
    with tenant_context(a.id):
        with tenant_context(a.id):
            assert current_org.get() == a.id
        assert current_org.get() == a.id
        assert db_tenant_value() == str(a.id)
    assert current_org.get() is None


def test_switching_org_inside_context_raises_mismatch(two_orgs):
    a, b = two_orgs
    with tenant_context(a.id):
        with pytest.raises(TenantMismatch):
            with tenant_context(b.id):
                pytest.fail("entered a second tenant inside an active one")
        # the outer context is intact
        assert current_org.get() == a.id
        assert db_tenant_value() == str(a.id)


def test_exception_inside_context_rolls_back_its_writes(two_orgs):
    from apps.core.models import Job

    a = two_orgs.a

    class Boom(Exception):
        pass

    with pytest.raises(Boom), tenant_context(a.id):
        Job.objects.create(type="qa.rollback")
        raise Boom
    with tenant_context(a.id):
        assert not Job.objects.filter(type="qa.rollback").exists()
    assert current_org.get() is None


def test_guc_is_cleared_after_context_exit_inside_outer_transaction(two_orgs):
    """Regression for QA defect core-tenancy-guc-leak (W1; Backend fix in W2): tenant_context() must restore the
    DB tenant setting on exit even when nested in an outer transaction (set_config(..., true) survives a savepoint
    release), otherwise code after the context in the same transaction still sees that tenant's rows via RLS."""
    a = two_orgs.a
    with transaction.atomic():  # e.g. a request/view transaction wrapping a service call
        with tenant_context(a.id):
            pass
        assert current_org.get() is None
        assert db_tenant_value() in ("", None)
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM orgs_membership")
            assert cur.fetchone()[0] == 0


# ----------------------------------------------------------------------------------- GUC lifetime on real connections
def test_guc_is_transaction_local_on_a_reused_connection():
    """Pool/CONN_MAX_AGE scenario: the setting must expire with the transaction that set it."""
    org_id = str(uuid.uuid4())
    with independent_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, org_id])
            cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
            assert cur.fetchone()[0] == org_id
        conn.commit()
        with conn.cursor() as cur:  # next transaction on the SAME pooled connection
            cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
            assert cur.fetchone()[0] in ("", None)
        conn.rollback()
        with conn.cursor() as cur:  # also after a rollback
            cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, org_id])
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
            assert cur.fetchone()[0] in ("", None)


def test_guc_does_not_leak_to_other_connections(two_orgs):
    a = two_orgs.a
    with tenant_context(a.id):
        assert db_tenant_value() == str(a.id)
        with independent_connection() as other, other.cursor() as cur:
            cur.execute("SELECT current_setting(%s, true)", [TENANT_GUC])
            assert cur.fetchone()[0] in ("", None)


def test_no_session_level_tenant_setting_anywhere_in_app_code():
    """MULTI_TENANCY_DESIGN §4.3: never SET / set_config(..., false) for the tenant setting."""
    guc = re.escape(TENANT_GUC)
    session_set = re.compile(rf"\bSET\s+(SESSION\s+)?{guc}\b", re.I)
    set_config_false = re.compile(r"set_config\s*\([^)]*,\s*(false|False|'false'|0)\s*\)", re.I)
    offenders = []
    for p in (BACKEND / "apps").rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if session_set.search(text) or set_config_false.search(text):
            offenders.append(p.relative_to(BACKEND).as_posix())
    assert offenders == []


# ----------------------------------------------------------------------------------- fail-closed ORM
def test_every_tenant_model_uses_the_fail_closed_manager():
    models = _tenant_models()
    assert models, "no TenantModel subclasses found"
    for model in models:
        assert isinstance(model._default_manager, TenantManager) or isinstance(
            model.objects, TenantManager
        ), model
        with pytest.raises(TenantContextMissing):
            model.objects.all()
        with pytest.raises(TenantContextMissing):
            model.objects.filter(pk=uuid.uuid4())


def test_save_without_context_fails_closed():
    from apps.core.models import Job

    with pytest.raises(TenantContextMissing):
        Job(type="qa.no_ctx", org_id=uuid.uuid4()).save()


def test_save_for_another_org_raises_mismatch(two_orgs):
    from apps.core.models import Job

    a, b = two_orgs
    with tenant_context(a.id):
        with pytest.raises(TenantMismatch):
            Job(type="qa.mismatch", org_id=b.id).save()
        job = Job(type="qa.autofill")
        job.save()
        assert job.org_id == a.id


def test_manager_and_rls_each_isolate_on_their_own(two_orgs):
    from apps.core.models import Job

    a, b = two_orgs
    job_a = make_job(a.org, user=a.owner)
    job_b = make_job(b.org, user=b.owner)
    with tenant_context(b.id):
        assert set(Job.objects.values_list("id", flat=True)) == {job_b.id}
        # second layer: the unscoped manager has NO ORM filter, Postgres RLS alone must hide org A
        assert set(Job.unscoped.values_list("id", flat=True)) == {job_b.id}
        assert Job.unscoped.filter(id=job_a.id).update(status="cancelled") == 0
        assert Job.unscoped.filter(id=job_a.id).delete()[0] == 0
    with tenant_context(a.id):
        assert Job.objects.get(id=job_a.id).status == "queued"


# ----------------------------------------------------------------------------------- TenantTask
def test_tenant_task_requires_org_id_keyword(two_orgs):
    with pytest.raises(ValueError):
        probe_task()
    with pytest.raises((ValueError, TypeError)):
        probe_task(str(two_orgs.a.id))  # positional org ids are never accepted


def test_tenant_task_runs_inside_the_org_and_sees_only_its_rows(two_orgs):
    a, b = two_orgs
    job_a = make_job(a.org, user=a.owner)
    job_b = make_job(b.org, user=b.owner)
    result = probe_task(org_id=str(b.id))
    assert result["org"] == str(b.id)
    assert result["guc"] == str(b.id)
    assert result["jobs"] == [str(job_b.id)]
    assert result["raw_orgs"] == [str(b.id)]
    assert str(job_a.id) not in str(result)
    assert current_org.get() is None


def test_tenant_task_eager_delay_path(two_orgs):
    a = two_orgs.a
    job_a = make_job(a.org)
    result = probe_task.delay(org_id=str(a.id)).get()
    assert result["jobs"] == [str(job_a.id)]


def test_tenant_task_refuses_a_different_active_tenant(two_orgs):
    a, b = two_orgs
    with tenant_context(a.id):
        with pytest.raises(RuntimeError):
            probe_task(org_id=str(b.id))
        assert probe_task(org_id=str(a.id))["org"] == str(a.id)


def test_tenant_task_skips_inactive_orgs(two_orgs):
    b = two_orgs.b
    set_org_fields(b.org, status="suspended")
    with pytest.raises(OrgInactive):
        probe_task(org_id=str(b.id))


def test_tenant_task_unknown_org_is_inactive(db):
    with pytest.raises(OrgInactive):
        probe_task(org_id=str(uuid.uuid4()))
    clear_db_tenant()


def test_long_running_task_body_is_outside_tenant_until_step(two_orgs):
    a = two_orgs.a
    result = long_probe_task(org_id=str(a.id))
    assert result["outside"] is None
    assert result["inside"] == str(a.id)
    assert result["guc"] == str(a.id)


def test_tenant_task_does_not_leak_actor_to_later_tasks(two_orgs):
    """Regression for QA defect core-tenanttask-actor-leak (W1; Backend fix in W2): TenantTask.__call__ must
    reset current_actor after the task, otherwise the next task on the same worker thread inherits the previous
    user as actor (wrong audit attribution)."""
    a = two_orgs.a
    token = current_actor.set(("system", None))
    try:
        result = probe_task(org_id=str(a.id), actor_id=str(a.owner.pk))
        assert result["actor"] == ("user", a.owner.pk)
        assert current_actor.get() == ("system", None)
    finally:
        current_actor.reset(token)


def test_raw_sql_without_context_sees_nothing(two_orgs):
    make_job(two_orgs.a.org)
    clear_db_tenant()
    with connection.cursor() as cur:
        for table in ("core_job", "orgs_membership", "orgs_role", "orgs_organization", "core_auditevent"):
            cur.execute(f'SELECT count(*) FROM "{table}"')
            assert cur.fetchone()[0] == 0, table


def test_cross_org_update_of_org_id_is_rejected(two_orgs):
    a, b = two_orgs
    job_b = make_job(b.org)
    with tenant_context(b.id):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
            cur.execute("UPDATE core_job SET org_id = %s WHERE id = %s", [str(a.id), str(job_b.id)])
