"""W4 (CONTRACT "W4 additions / Permissions"): seed the ML, Customer 360 and Product 360 permissions into the system
roles of EVERY existing org, and add the pre-tenant lookup that tells a member of a SUSPENDED org apart from a
non-member (suspended members get 403 `org_suspended`, everyone else keeps 404).

The data step is idempotent (add-only, `ignore_conflicts`): it never removes a grant and never touches custom roles.
New orgs get the same grants from `permissions_catalog.SYSTEM_ROLES` through `services.seed_system_roles`. The grant
map is FROZEN here (a migration must not depend on code that changes later).

Migrations run as nexora_owner, which is subject to the FORCE'd tenant RLS. The org ids are therefore listed as
nexora_definer (BYPASSRLS, SELECT on orgs_organization since 0002) and every org's rows are written inside that org's
transaction-local tenant setting, exactly like a request would.
"""

from django.db import migrations

from apps.core.tenancy.constants import TENANT_GUC

W4_PERMISSIONS = {
    "ml.view": "View ML projects and models",
    "ml.manage": "Run EDA, train and register models",
    "ml.promote": "Promote models to production",
    "ml.predict": "Call the prediction API",
    "customers.view": "View customer profiles",
    "customers.manage": "Import/resolve/segment/score customers",
    "customers.review": "Review identity matches",
    "products.view": "View product master data",
    "products.manage": "Import/normalize/enrich products",
    "products.review": "Review product matches and corrections",
}
_ALL = frozenset(W4_PERMISSIONS)
_VIEW = frozenset({"ml.view", "customers.view", "products.view"})
_WORK = _VIEW | {"ml.manage", "customers.manage", "customers.review", "products.manage", "products.review"}

# Owner/Admin: all. Analyst (and the roles built on it): view + manage + review, never ml.promote.
# Viewer and Auditor: *.view. Data scientist additionally keeps `ml.predict` (API-key scope).
W4_GRANTS = {
    "owner": _ALL,
    "admin": _ALL,
    "analyst": _WORK,
    "data_engineer": _WORK,
    "data_scientist": _WORK | {"ml.predict"},
    "viewer": _VIEW,
    "auditor": _VIEW,
}

FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION nexora_member_org_status(p_user uuid, p_slug text)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
  SELECT o.status::text
  FROM orgs_organization o
  JOIN orgs_membership m ON m.org_id = o.id AND m.user_id = p_user AND m.status = 'active'
  JOIN accounts_user u ON u.id = p_user AND u.is_active
  WHERE o.slug = p_slug AND o.status <> 'deleted'
$$;

ALTER FUNCTION nexora_member_org_status(uuid, text) OWNER TO nexora_definer;
REVOKE ALL ON FUNCTION nexora_member_org_status(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION nexora_member_org_status(uuid, text) TO nexora_app, nexora_ops;
"""

REVERSE_SQL = "DROP FUNCTION IF EXISTS nexora_member_org_status(uuid, text);"


def ensure_catalog(Permission) -> dict:
    perms = {p.codename: p for p in Permission.objects.filter(codename__in=list(W4_PERMISSIONS))}
    for code, desc in W4_PERMISSIONS.items():
        if code not in perms:
            perms[code], _ = Permission.objects.get_or_create(codename=code, defaults={"description": desc})
    return perms


def grant_for_current_org(apps, perms: dict) -> int:
    """Add the missing W4 grants to the system roles of the org in the ACTIVE tenant setting. Returns rows added."""
    Role = apps.get_model("orgs", "Role")
    RolePermission = apps.get_model("orgs", "RolePermission")
    added = 0
    for role in Role.objects.filter(is_system=True, key__in=list(W4_GRANTS)):
        have = set(RolePermission.objects.filter(role_id=role.id).values_list("permission__codename", flat=True))
        missing = sorted(W4_GRANTS[role.key] - have)
        RolePermission.objects.bulk_create(
            [RolePermission(org_id=role.org_id, role_id=role.id, permission=perms[c]) for c in missing],
            ignore_conflicts=True,
        )
        added += len(missing)
    return added


def _org_ids(cursor) -> list:
    """Every org id, read as nexora_definer (BYPASSRLS); the previous role is restored afterwards."""
    cursor.execute("SELECT current_user")
    me = cursor.fetchone()[0]
    cursor.execute("SET LOCAL ROLE nexora_definer")
    try:
        cursor.execute("SELECT id FROM orgs_organization ORDER BY created_at")
        return [row[0] for row in cursor.fetchall()]
    finally:
        cursor.execute(f'SET LOCAL ROLE "{me}"')


def forwards(apps, schema_editor):
    perms = ensure_catalog(apps.get_model("orgs", "Permission"))
    with schema_editor.connection.cursor() as cur:
        org_ids = _org_ids(cur)
        for org_id in org_ids:
            cur.execute("SELECT set_config(%s, %s, true)", [TENANT_GUC, str(org_id)])
            grant_for_current_org(apps, perms)
        cur.execute("SELECT set_config(%s, '', true)", [TENANT_GUC])


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0003_aipolicyconsent_invitation"),
        ("accounts", "0002_apikey"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS_SQL, REVERSE_SQL),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
