"""Database privilege management, applied after every migrate (works in dev, CI and test DBs).

Tables are owned by nexora_owner. nexora_app gets DML on everything; append-only tables
(COMMENT 'nexora:append_only') lose UPDATE/DELETE. nexora_ops and nexora_definer (owner of
SECURITY DEFINER lookup functions) get read/write as needed.
"""

from __future__ import annotations

import logging

from django.db import DEFAULT_DB_ALIAS, connections

from .tenancy.operations import APPEND_ONLY_COMMENT

log = logging.getLogger(__name__)

DURABLE_ALIAS = "durable"


def durable_alias() -> str:
    """DB alias for rows that must be visible/committed independently of the caller's transaction (job events,
    logs): "durable" (a separate connection, same RLS-bound app role) in production.

    Falls back to "default" when the alias is not configured, or when "durable" is a test MIRROR of "default"
    while "default" is held open by a django TestCase transaction (the mirror connection could not see the
    uncommitted org rows the new row references)."""
    if DURABLE_ALIAS not in connections.databases:
        return DEFAULT_DB_ALIAS
    mirror = (connections[DURABLE_ALIAS].settings_dict.get("TEST") or {}).get("MIRROR")
    if mirror == DEFAULT_DB_ALIAS and any(
        getattr(block, "_from_testcase", False) for block in connections[DEFAULT_DB_ALIAS].atomic_blocks
    ):
        return DEFAULT_DB_ALIAS
    return DURABLE_ALIAS


def apply_grants(using: str = "default") -> bool:
    conn = connections[using]
    with conn.cursor() as cur:
        cur.execute("SELECT pg_has_role(current_user, 'nexora_owner', 'MEMBER')")
        if not cur.fetchone()[0]:
            return False  # not running as the owner (e.g. app role) — nothing to do
        cur.execute(
            """
            GRANT USAGE ON SCHEMA public TO nexora_app, nexora_ops, nexora_definer;
            GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nexora_app, nexora_ops;
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexora_definer;
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nexora_app, nexora_ops;
            """
        )
        cur.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r','p') AND obj_description(c.oid, 'pg_class') = %s
            """,
            [APPEND_ONLY_COMMENT],
        )
        for (table,) in cur.fetchall():
            cur.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON "{table}" FROM nexora_app')
    return True


def on_post_migrate(sender, using="default", **kwargs):
    try:
        apply_grants(using)
    except Exception:  # pragma: no cover - never break migrate on grant issues, but log loudly
        log.exception("nexora: applying grants failed")
        raise
