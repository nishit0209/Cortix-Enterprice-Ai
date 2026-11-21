"""API-key lookup also returns keys of SUSPENDED orgs, with the org status, so authentication can answer
403 `org_suspended` (the same answer a session member gets) instead of a generic 401. Keys of deleted orgs,
revoked members and inactive users are still never returned."""

from importlib import import_module

from django.db import migrations

_PREV = import_module("apps.accounts.migrations.0002_apikey")

FUNCTIONS_SQL = """
DROP FUNCTION IF EXISTS nexora_api_key_lookup(text);
CREATE FUNCTION nexora_api_key_lookup(p_prefix text)
RETURNS TABLE(api_key_id uuid, org_id uuid, org_slug text, key_hash bytea, scopes text[],
              expires_at timestamptz, revoked_at timestamptz, created_by uuid, creator_permissions text[],
              last_used_at timestamptz, org_status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_catalog AS $$
  SELECT k.id, k.org_id, o.slug::text, k.key_hash, k.scopes::text[], k.expires_at, k.revoked_at,
         k.created_by_id,
         COALESCE(ARRAY(SELECT p.codename::text FROM orgs_rolepermission rp
                        JOIN orgs_permission p ON p.id = rp.permission_id
                        WHERE rp.role_id = m.role_id ORDER BY p.codename), ARRAY[]::text[]),
         k.last_used_at, o.status::text
  FROM accounts_apikey k
  JOIN orgs_organization o ON o.id = k.org_id AND o.status IN ('active', 'suspended')
  JOIN orgs_membership m ON m.org_id = k.org_id AND m.user_id = k.created_by_id AND m.status = 'active'
  JOIN accounts_user u ON u.id = k.created_by_id AND u.is_active
  WHERE k.prefix = p_prefix
$$;

ALTER FUNCTION nexora_api_key_lookup(text) OWNER TO nexora_definer;
REVOKE ALL ON FUNCTION nexora_api_key_lookup(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION nexora_api_key_lookup(text) TO nexora_app, nexora_ops;
"""

REVERSE_SQL = "DROP FUNCTION IF EXISTS nexora_api_key_lookup(text);\n" + _PREV.FUNCTIONS_SQL


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_apikey"),
        ("orgs", "0004_w4_permissions_and_org_status"),
    ]

    operations = [migrations.RunSQL(FUNCTIONS_SQL, REVERSE_SQL)]
