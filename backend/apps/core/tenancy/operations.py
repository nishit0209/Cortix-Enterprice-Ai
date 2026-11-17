"""Migration operations for tenant isolation and append-only tables.

    from apps.core.tenancy.operations import EnableTenantRLS, MakeAppendOnly
    operations = [migrations.CreateModel(...), EnableTenantRLS("datasets_dataset")]
"""

from __future__ import annotations

from django.db.migrations.operations.base import Operation

from .constants import TENANT_GUC

APPEND_ONLY_COMMENT = "nexora:append_only"

_TENANT_EXPR = f"NULLIF(current_setting('{TENANT_GUC}', true), '')::uuid"


def rls_sql(table: str, column: str = "org_id") -> str:
    return (
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;'
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;'
        f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";'
        f'CREATE POLICY tenant_isolation ON "{table}" '
        f"USING ({column} = {_TENANT_EXPR}) WITH CHECK ({column} = {_TENANT_EXPR});"
    )


def rls_reverse_sql(table: str) -> str:
    return (
        f'DROP POLICY IF EXISTS tenant_isolation ON "{table}";'
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY;'
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY;'
    )


class EnableTenantRLS(Operation):
    """Enable + FORCE RLS with the canonical tenant policy on `table` (column default org_id)."""

    reversible = True
    reduces_to_sql = True

    def __init__(self, table: str, column: str = "org_id"):
        self.table = table
        self.column = column

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute(rls_sql(self.table, self.column))

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute(rls_reverse_sql(self.table))

    def describe(self):
        return f"Enable tenant RLS on {self.table}"

    def deconstruct(self):
        kwargs = {"table": self.table}
        if self.column != "org_id":
            kwargs["column"] = self.column
        return (self.__class__.__qualname__, [], kwargs)


class MakeAppendOnly(Operation):
    """Mark a table append-only: trigger forbids UPDATE/DELETE (except nexora_ops sealing) and the
    post-migrate grant step revokes UPDATE/DELETE from nexora_app."""

    reversible = True
    reduces_to_sql = True

    def __init__(self, table: str, allow_update_columns: tuple[str, ...] = ()):
        self.table = table
        self.allow_update_columns = tuple(allow_update_columns)

    def state_forwards(self, app_label, state):
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        t = self.table
        schema_editor.execute(
            f"""
            CREATE OR REPLACE FUNCTION nexora_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF TG_OP = 'UPDATE' AND current_user = 'nexora_ops' THEN RETURN NEW; END IF;
              RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
            END $$;
            DROP TRIGGER IF EXISTS append_only ON "{t}";
            CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON "{t}"
              FOR EACH ROW EXECUTE FUNCTION nexora_forbid_mutation();
            COMMENT ON TABLE "{t}" IS '{APPEND_ONLY_COMMENT}';
            """
        )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute(f'DROP TRIGGER IF EXISTS append_only ON "{self.table}"; COMMENT ON TABLE "{self.table}" IS NULL;')

    def describe(self):
        return f"Make {self.table} append-only"

    def deconstruct(self):
        return (self.__class__.__qualname__, [], {"table": self.table})
