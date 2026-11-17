"""Static tenancy checks (MULTI_TENANCY_DESIGN §7.7) — no database needed.

* every TenantModel's table gets EnableTenantRLS in some migration (read from the migration graph on disk);
* every UniqueConstraint / unique_together on a tenant model includes `org`;
* no tenant model declares a field-level `unique=True` (would be global across orgs);
* tenant views in app urls subclass the tenancy mixin.
"""

from __future__ import annotations

import pytest
from django.apps import apps as django_apps
from django.db.migrations.loader import MigrationLoader

from apps.core.tenancy.models import TenantModel
from apps.core.tenancy.operations import EnableTenantRLS, MakeAppendOnly
from testing.routes import tenant_routes


def _tenant_models():
    return [m for m in django_apps.get_models() if issubclass(m, TenantModel)]


@pytest.fixture(scope="module")
def migration_ops():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    rls, append_only = {}, set()
    for (app_label, name), migration in loader.disk_migrations.items():
        for op in migration.operations:
            if isinstance(op, EnableTenantRLS):
                rls[op.table] = op.column
            elif isinstance(op, MakeAppendOnly):
                append_only.add(op.table)
    return rls, append_only


def test_every_tenant_model_enables_rls_in_its_migrations(migration_ops):
    rls, _ = migration_ops
    missing = sorted(m._meta.db_table for m in _tenant_models() if m._meta.db_table not in rls)
    assert missing == [], f"add EnableTenantRLS(...) to the migrations of: {missing}"


def test_rls_operations_target_the_org_column(migration_ops):
    rls, _ = migration_ops
    wrong = {t: c for t, c in rls.items() if c != "org_id" and t != "orgs_organization"}
    assert wrong == {}


def test_audit_log_is_append_only(migration_ops):
    _, append_only = migration_ops
    assert "core_auditevent" in append_only


def test_tenant_unique_constraints_include_org():
    problems = []
    for model in _tenant_models():
        opts = model._meta
        for c in opts.constraints:
            fields = getattr(c, "fields", ()) or ()
            if c.__class__.__name__ == "UniqueConstraint" and "org" not in fields and "org_id" not in fields:
                problems.append(f"{opts.label}.{c.name}: {fields or c.expressions}")
        for together in opts.unique_together:
            if "org" not in together and "org_id" not in together:
                problems.append(f"{opts.label}.unique_together {together}")
        for f in opts.local_fields:
            if f.unique and not f.primary_key:
                problems.append(f"{opts.label}.{f.name}: unique=True is global across orgs")
    assert problems == []


def test_tenant_routes_use_the_tenancy_mixin():
    from apps.core.tenancy.views import TenantScopedMixin

    allowed_function_views = {"job-events"}  # SSE: does its own membership check (apps.core.sse._check_access)
    problems = []
    for r in tenant_routes():
        cls = r.view_class
        if cls is None:
            if r.name not in allowed_function_views:
                problems.append(f"{r.id}: function view under /orgs/<org>/ without TenantScopedMixin")
        elif not issubclass(cls, TenantScopedMixin):
            problems.append(f"{r.id}: {cls.__module__}.{cls.__name__} does not use TenantScopedMixin")
    assert problems == []
