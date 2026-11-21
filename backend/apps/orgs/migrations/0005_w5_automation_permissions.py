"""W5: seed the automation permissions into the system roles of EVERY existing org (idempotent, add-only, never
touches custom roles). New orgs get the same grants from `permissions_catalog.SYSTEM_ROLES`. The grant map is FROZEN
here; the org walk reuses the W4 migration's helpers (rows written inside each org's tenant setting)."""

from importlib import import_module

from django.db import migrations

from apps.core.tenancy.constants import TENANT_GUC

_W4 = import_module("apps.orgs.migrations.0004_w4_permissions_and_org_status")

W5_PERMISSIONS = {
    "automation.view": "View workflows and runs",
    "automation.manage": "Create/edit workflows, triggers and agents",
    "automation.run": "Run workflows",
    "approvals.decide": "Approve or reject approval requests",
}
_VIEW = frozenset({"automation.view"})
W5_GRANTS = {
    "owner": frozenset(W5_PERMISSIONS),
    "admin": frozenset(W5_PERMISSIONS),
    "data_engineer": _VIEW | {"automation.manage", "automation.run"},
    "data_scientist": _VIEW | {"automation.run"},
    "analyst": _VIEW,
    "auditor": _VIEW,
}


def forwards(apps, schema_editor):
    Permission = apps.get_model("orgs", "Permission")
    Role = apps.get_model("orgs", "Role")
    RolePermission = apps.get_model("orgs", "RolePermission")
    perms = {}
    for code, desc in W5_PERMISSIONS.items():
        perms[code], _ = Permission.objects.get_or_create(codename=code, defaults={"description": desc})
    with schema_editor.connection.cursor() as cur:
        for org_id in _W4._org_ids(cur):
            cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, str(org_id)])
            for role in Role.objects.filter(is_system=True, key__in=list(W5_GRANTS)):
                have = set(RolePermission.objects.filter(role_id=role.id)
                           .values_list("permission__codename", flat=True))
                RolePermission.objects.bulk_create(
                    [RolePermission(org_id=role.org_id, role_id=role.id, permission=perms[c])
                     for c in sorted(W5_GRANTS[role.key] - have)],
                    ignore_conflicts=True,
                )
        cur.execute("SELECT set_config(%s, '', true)", [TENANT_GUC])


class Migration(migrations.Migration):
    dependencies = [("orgs", "0004_w4_permissions_and_org_status")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
