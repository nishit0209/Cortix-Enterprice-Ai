"""RLS for org tables + SECURITY DEFINER pre-tenant lookup functions (owned by nexora_definer)."""

from django.db import migrations

from apps.core.tenancy.operations import EnableTenantRLS

FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION nexora_membership_check(p_user uuid, p_slug text)
RETURNS TABLE(org_id uuid, role_key text, permissions text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
  SELECT o.id, r.key,
         COALESCE(ARRAY(SELECT p.codename FROM orgs_rolepermission rp
                        JOIN orgs_permission p ON p.id = rp.permission_id
                        WHERE rp.role_id = r.id ORDER BY p.codename), ARRAY[]::text[])
  FROM orgs_organization o
  JOIN orgs_membership m ON m.org_id = o.id AND m.user_id = p_user AND m.status = 'active'
  JOIN orgs_role r ON r.id = m.role_id
  JOIN accounts_user u ON u.id = p_user AND u.is_active
  WHERE o.slug = p_slug AND o.status = 'active'
$$;

CREATE OR REPLACE FUNCTION nexora_user_orgs(p_user uuid)
RETURNS TABLE(org_id uuid, slug text, name text, role_key text, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
  SELECT o.id, o.slug::text, o.name::text, r.key::text, o.status::text
  FROM orgs_membership m
  JOIN orgs_organization o ON o.id = m.org_id AND o.status <> 'deleted'
  JOIN orgs_role r ON r.id = m.role_id
  WHERE m.user_id = p_user AND m.status = 'active'
  ORDER BY o.name
$$;

CREATE OR REPLACE FUNCTION nexora_slug_taken(p_slug text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
  SELECT EXISTS (SELECT 1 FROM orgs_organization WHERE slug = p_slug)
$$;

ALTER FUNCTION nexora_membership_check(uuid, text) OWNER TO nexora_definer;
ALTER FUNCTION nexora_user_orgs(uuid) OWNER TO nexora_definer;
ALTER FUNCTION nexora_slug_taken(text) OWNER TO nexora_definer;
REVOKE ALL ON FUNCTION nexora_membership_check(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION nexora_user_orgs(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION nexora_slug_taken(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION nexora_membership_check(uuid, text) TO nexora_app, nexora_ops;
GRANT EXECUTE ON FUNCTION nexora_user_orgs(uuid) TO nexora_app, nexora_ops;
GRANT EXECUTE ON FUNCTION nexora_slug_taken(text) TO nexora_app, nexora_ops;
GRANT SELECT ON orgs_organization, orgs_membership, orgs_role, orgs_rolepermission, orgs_permission,
      accounts_user TO nexora_definer;
"""

REVERSE_SQL = """
DROP FUNCTION IF EXISTS nexora_membership_check(uuid, text);
DROP FUNCTION IF EXISTS nexora_user_orgs(uuid);
DROP FUNCTION IF EXISTS nexora_slug_taken(text);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("orgs", "0001_initial"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        EnableTenantRLS("orgs_organization", column="id"),
        EnableTenantRLS("orgs_role"),
        EnableTenantRLS("orgs_rolepermission"),
        EnableTenantRLS("orgs_membership"),
        migrations.RunSQL(FUNCTIONS_SQL, REVERSE_SQL),
    ]
