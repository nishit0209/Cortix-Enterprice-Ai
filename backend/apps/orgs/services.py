"""Public service API of apps.orgs."""

from __future__ import annotations

import base64
import os
import re
from uuid import UUID

from django.db import connection, transaction
from django.utils.text import slugify

from apps.core import audit
from apps.core.signals import org_created
from apps.core.tenancy import tenant_context
from apps.core.tenancy.models import uuid7

from .models import Membership, Organization, Permission, Role, RolePermission
from .permissions_catalog import PERMISSIONS, SYSTEM_ROLES


def ensure_permission_catalog() -> dict[str, Permission]:
    existing = {p.codename: p for p in Permission.objects.all()}
    for code, desc in PERMISSIONS.items():
        if code not in existing:
            existing[code] = Permission.objects.create(codename=code, description=desc)
    return existing


def seed_system_roles(org: Organization) -> dict[str, Role]:
    perms = ensure_permission_catalog()
    roles: dict[str, Role] = {}
    for key, spec in SYSTEM_ROLES.items():
        role, _ = Role.objects.get_or_create(
            key=key, defaults={"name": spec["name"], "is_system": True, "org_id": org.id}
        )
        have = set(RolePermission.objects.filter(role=role).values_list("permission__codename", flat=True))
        RolePermission.objects.bulk_create(
            [RolePermission(org_id=org.id, role=role, permission=perms[c]) for c in sorted(spec["permissions"] - have)]
        )
        roles[key] = role
    return roles


def _short_id() -> str:
    return base64.b32encode(os.urandom(5)).decode().lower()[:8]


def unique_slug(name: str) -> str:
    base = slugify(name)[:40] or "org"
    base = re.sub(r"^[^a-z0-9]+", "", base) or "org"
    with connection.cursor() as cur:
        cur.execute("SELECT nexora_slug_taken(%s)", [base])
        if not cur.fetchone()[0]:
            return base
    return f"{base}-{_short_id()[:4]}"


def create_organization(*, name: str, owner, slug: str | None = None, request=None) -> Organization:
    """Create an org with seeded roles and the owner membership. AI policy is ALWAYS synthetic_only."""
    org_id = uuid7()
    slug = slug or unique_slug(name)
    with transaction.atomic(), tenant_context(org_id):
        org = Organization.objects.create(
            id=org_id, name=name.strip()[:120], slug=slug, short_id=_short_id(), ai_data_policy="synthetic_only"
        )
        roles = seed_system_roles(org)
        Membership.objects.create(org_id=org.id, user=owner, role=roles["owner"])
        audit.record("org.create", target=org, metadata={"name": org.name, "slug": org.slug},
                     actor=("user", owner.pk), request=request)
        transaction.on_commit(lambda: _send_created(org.id))
    return org


def _send_created(org_id: UUID) -> None:
    with tenant_context(org_id):
        org_created.send(sender=Organization, org_id=org_id)


def user_orgs(user_id) -> list[dict]:
    with connection.cursor() as cur:
        cur.execute("SELECT org_id, slug, name, role_key, status FROM nexora_user_orgs(%s)", [str(user_id)])
        rows = cur.fetchall()
    return [{"id": r[0], "slug": r[1], "name": r[2], "role": r[3], "status": r[4]} for r in rows]


def ai_policy_state() -> dict:
    """CONTRACT used by apps.ai's policy gate. Inside a tenant context.

    Returns {"policy": AIDataPolicy value, "consent_valid": bool}. A policy above synthetic_only is only
    effective with a valid consent; otherwise callers must treat the org as synthetic_only.
    (Wave-0 stub: Backend agent implements consent records in wave 1 — keep this signature.)
    """
    org = get_current_org()
    return {"policy": org.ai_data_policy, "consent_valid": org.ai_data_policy == "synthetic_only"}


def get_current_org() -> Organization:
    """Inside a tenant context the self-policy makes exactly one org visible."""
    return Organization.objects.get()
