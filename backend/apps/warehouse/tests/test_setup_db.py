"""The warehouse on the local PostgreSQL 16/17 created by scripts/bootstrap_local_db.py (DB-backed, nexora_wh).

`check_setup()` against the real roles, the PG16+ CREATEROLE grant path (the loader holds ADMIN OPTION on the reader
role it created; the NOINHERIT executor is a member with SET but without INHERIT) and `manage.py check_warehouse
--provision-test`. Objects use the test-session prefix and are dropped at session end.
"""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from apps.core.tenancy import tenant_context
from apps.warehouse import naming, services
from apps.warehouse.db import executor_connection, loader_connection

pytestmark = pytest.mark.django_db

MEMBERSHIP_SQL = (
    "SELECT m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m "
    "JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member "
    "WHERE r.rolname = %s AND u.rolname = %s"
)


def test_bootstrap_roles_pass_the_setup_check():
    report = services.check_setup()
    assert report.ok, report.problems
    assert report.server_version is not None and report.server_version >= 160000
    assert report.checks["loader_createrole"] and not report.checks["loader_superuser"]
    assert report.checks["executor_exists"] and not report.checks["executor_inherit"]


def test_the_loader_creates_and_grants_the_reader_role_with_pg16_semantics(two_orgs):
    _alice, org_a, _bob, _org_b = two_orgs
    with tenant_context(org_a.id):
        binding = services.ensure_provisioned(org_a.id)
    executor = naming.executor_login_role()
    with loader_connection(autocommit=True) as conn:
        loader = conn.execute("SELECT current_user").fetchone()[0]
        admin_option, _inherit, _set = conn.execute(MEMBERSHIP_SQL, [binding.reader_role, loader]).fetchone()
        member = conn.execute(MEMBERSHIP_SQL, [binding.reader_role, executor]).fetchone()
    assert admin_option is True  # granted automatically to the creating CREATEROLE role (PG16+)
    assert member is not None and member[1] is False and member[2] is True  # SET without INHERIT
    # idempotent: provisioning again neither fails nor duplicates the grant
    services.provision(org_a.id)
    with loader_connection(autocommit=True) as conn:
        assert len(conn.execute(MEMBERSHIP_SQL, [binding.reader_role, executor]).fetchall()) == 1
    # the executor reaches the org schema only through SET ROLE
    with executor_connection() as conn:
        conn.execute(f'SET LOCAL ROLE "{naming.check_ident(binding.reader_role)}"')
        assert conn.execute("SELECT has_schema_privilege(%s, 'USAGE')", [binding.wh_schema]).fetchone()[0] is True
        conn.rollback()
    with executor_connection() as conn:
        assert conn.execute("SELECT has_schema_privilege(%s, 'USAGE')", [binding.wh_schema]).fetchone()[0] is False
        conn.rollback()


def test_check_warehouse_command_runs_a_provision_test():
    out = io.StringIO()
    call_command("check_warehouse", "--provision-test", stdout=out)
    text = out.getvalue()
    assert "Warehouse roles OK." in text and "provision test OK" in text
    with loader_connection(autocommit=True) as conn:  # the throwaway schema and role are gone
        assert conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname LIKE 't_check_%'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'wh_q_t_check_%'").fetchone()[0] == 0
