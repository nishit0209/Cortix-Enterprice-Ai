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


# Intentionally GLOBAL uniqueness on tenant models (explicit, reviewed allowlist — keep it minimal).
# Each entry is a random, high-entropy lookup token that is resolved BEFORE the tenant is known, through a
# SECURITY DEFINER function that returns only the owning org's row. Uniqueness across orgs is required there
# (the token alone must identify exactly one row) and leaks nothing: the value is random, never derived from
# tenant data, and a collision attempt only yields "already exists" for a random string.
# Anything not listed here still fails, including new fields on these same models.
GLOBAL_LOOKUP_TOKENS: dict[tuple[str, str], str] = {
    ("accounts.ApiKey", "prefix"): (
        "8-char random API-key prefix (`nxk_<prefix>_<secret>`), resolved pre-tenant by the SECURITY DEFINER "
        "function nexora_api_key_lookup(prefix); the secret is verified by HMAC afterwards"
    ),
    ("orgs.Invitation", "token_hash"): (
        "sha256 of a random invitation token, resolved pre-tenant by the SECURITY DEFINER function "
        "nexora_invitation_by_token(bytea)"
    ),
}


def _unique_constraint_problems() -> list[str]:
    problems = []
    for model in _tenant_models():
        opts = model._meta
        for c in opts.constraints:
            fields = getattr(c, "fields", ()) or ()
            if c.__class__.__name__ == "UniqueConstraint" and "org" not in fields and "org_id" not in fields:
                if len(fields) == 1 and (opts.label, fields[0]) in GLOBAL_LOOKUP_TOKENS:
                    continue
                problems.append(f"{opts.label}.{c.name}: {fields or c.expressions}")
        for together in opts.unique_together:
            if "org" not in together and "org_id" not in together:
                problems.append(f"{opts.label}.unique_together {together}")
        for f in opts.local_fields:
            if f.unique and not f.primary_key and (opts.label, f.name) not in GLOBAL_LOOKUP_TOKENS:
                problems.append(f"{opts.label}.{f.name}: unique=True is global across orgs")
    return problems


def test_tenant_unique_constraints_include_org():
    assert _unique_constraint_problems() == []


def test_global_lookup_token_allowlist_is_exact_and_still_needed():
    """Every allowlisted token must exist, really be globally unique, and be the only global unique field."""
    by_label = {m._meta.label: m for m in _tenant_models()}
    assert set(GLOBAL_LOOKUP_TOKENS) == {("accounts.ApiKey", "prefix"), ("orgs.Invitation", "token_hash")}
    stale = []
    for (label, field_name), reason in GLOBAL_LOOKUP_TOKENS.items():
        assert reason.strip(), f"{label}.{field_name}: an allowlist entry needs a documented reason"
        model = by_label.get(label)
        if model is None:
            stale.append(f"{label}: not a tenant model any more")
            continue
        field = model._meta.get_field(field_name)
        single_field_uniques = {
            c.fields[0]
            for c in model._meta.constraints
            if c.__class__.__name__ == "UniqueConstraint" and len(getattr(c, "fields", ()) or ()) == 1
        }
        if not (field.unique or field_name in single_field_uniques):
            stale.append(f"{label}.{field_name}: no longer globally unique; remove it from the allowlist")
    assert stale == []


def test_unique_allowlist_does_not_hide_other_fields(monkeypatch):
    """Self-test: a new global unique field on an allowlisted model is still reported."""
    from apps.accounts.models import ApiKey

    name_field = ApiKey._meta.get_field("name")
    monkeypatch.setattr(name_field, "_unique", True)
    monkeypatch.setitem(
        name_field.__dict__, "unique", True
    )  # Field.unique is a cached_property in Django 5.x
    assert any(p.startswith("accounts.ApiKey.name:") for p in _unique_constraint_problems())


def test_tenant_routes_use_the_tenancy_mixin():
    from apps.core.tenancy.views import TenantScopedMixin

    # SSE: does its own membership check (apps.core.sse._check_access)
    allowed_function_views = {"job-events"}
    problems = []
    for r in tenant_routes():
        cls = r.view_class
        if cls is None:
            if r.name not in allowed_function_views:
                problems.append(f"{r.id}: function view under /orgs/<org>/ without TenantScopedMixin")
        elif not issubclass(cls, TenantScopedMixin):
            problems.append(f"{r.id}: {cls.__module__}.{cls.__name__} does not use TenantScopedMixin")
    assert problems == []
