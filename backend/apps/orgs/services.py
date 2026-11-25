"""Public service API of apps.orgs.

Contracts used by other agents (keep signatures stable):
  * ai_policy_state()      -> {"policy": AIDataPolicy value, "consent_valid": bool}   (apps.ai policy gate)
  * get_current_org()      -> Organization                                            (inside a tenant context)
  * create_organization()  / user_orgs()
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from apps.core import audit
from apps.core.exceptions import (
    AIPolicyConsentRequired,
    AIPolicyTierForbidden,
    Conflict,
    Forbidden,
    NexoraError,
)
from apps.core.signals import org_created
from apps.core.tenancy import TenantInfo, tenant_context
from apps.core.tenancy.models import uuid7

from . import ai_consent
from .models import (
    AI_DATA_POLICIES,
    AIPolicyConsent,
    Invitation,
    Membership,
    Organization,
    Permission,
    Role,
    RolePermission,
)
from .permissions_catalog import PERMISSIONS, SYSTEM_ROLES

log = logging.getLogger(__name__)

SYNTHETIC_ONLY = "synthetic_only"
POLICY_ORDER = [p for p, _ in AI_DATA_POLICIES]  # least -> most permissive
PAID_ONLY_POLICIES = frozenset({"aggregates_only", "full"})
INVITATION_TTL = timedelta(days=7)
OWNER = "owner"


# --- organizations ---------------------------------------------------------------------------------
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
    if len(base) < 2:
        base = f"{base}-org"
    with connection.cursor() as cur:
        cur.execute("SELECT nexora_slug_taken(%s)", [base])
        if not cur.fetchone()[0]:
            return base
    for _ in range(10):
        candidate = f"{base}-{_short_id()[:4]}"
        with connection.cursor() as cur:
            cur.execute("SELECT nexora_slug_taken(%s)", [candidate])
            if not cur.fetchone()[0]:
                return candidate
    return f"{base}-{_short_id()}"


def create_organization(*, name: str, owner, slug: str | None = None, request=None) -> Organization:
    """Create an org with seeded roles and the owner membership. AI policy is ALWAYS synthetic_only."""
    org_id = uuid7()
    slug = slug or unique_slug(name)
    with transaction.atomic(), tenant_context(org_id):
        org = Organization.objects.create(
            id=org_id, name=name.strip()[:120], slug=slug, short_id=_short_id(), ai_data_policy=SYNTHETIC_ONLY
        )
        roles = seed_system_roles(org)
        Membership.objects.create(org_id=org.id, user=owner, role=roles[OWNER])
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


def get_current_org() -> Organization:
    """Inside a tenant context the self-policy makes exactly one org visible."""
    return Organization.objects.get()


def org_payload(org: Organization, info: TenantInfo) -> dict:
    """The CONTRACT `Org` shape (+ `settings`). Inside the org's tenant context.

    `ai_data_policy` is the EFFECTIVE policy: a configured policy without a valid consent reads as
    synthetic_only (see `GET /ai-policy` for the configured value and consent status).
    """
    return {
        "id": str(org.id),
        "slug": org.slug,
        "name": org.name,
        "plan": org.plan,
        "status": org.status,
        "region": org.region,
        "ai_data_policy": effective_ai_policy(org),
        "settings": org.settings or {},
        "created_at": org.created_at,
        "role": info.role_key,
        "permissions": sorted(info.permissions),
    }


MAX_SETTINGS_BYTES = 16 * 1024


def update_organization(org: Organization, *, name: str | None = None, settings_patch: dict | None = None,
                        request=None) -> Organization:
    """PATCH semantics: `settings` keys are shallow-merged; a null value removes the key.
    The AI data policy is never writable here (consent flow only)."""
    import json

    changes: dict = {}
    if name is not None and name.strip() and name.strip() != org.name:
        changes["name"] = [org.name, name.strip()[:120]]
        org.name = name.strip()[:120]
    if settings_patch:
        merged = dict(org.settings or {})
        for k, v in settings_patch.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        if len(json.dumps(merged, default=str)) > MAX_SETTINGS_BYTES:
            raise ValidationError({"settings": ["Settings are too large (max 16 KB)."]})
        if merged != (org.settings or {}):
            changes["settings"] = sorted(settings_patch)
            org.settings = merged
    if changes:
        org.save(update_fields=["name", "settings", "updated_at"])
        audit.record("org.update", target=org, changes=changes, request=request)
    return org


# --- AI data policy (security critical) ------------------------------------------------------------
def policy_allowed_for_tier(policy: str, tier: str | None = None) -> bool:
    tier = tier or settings.GEMINI_TIER
    return not (policy in PAID_ONLY_POLICIES and tier == "free")


def allowed_policy_values(tier: str | None = None) -> list[str]:
    return [p for p in POLICY_ORDER if policy_allowed_for_tier(p, tier)]


def latest_consent(policy: str) -> AIPolicyConsent | None:
    """Latest ledger row (grant or revoke) for `policy` in the current org."""
    return AIPolicyConsent.objects.filter(policy=policy).order_by("-created_at", "-id").first()


def consent_is_valid(policy: str) -> bool:
    if policy == SYNTHETIC_ONLY:
        return True
    if policy not in POLICY_ORDER or not policy_allowed_for_tier(policy):
        return False
    latest = latest_consent(policy)
    return bool(
        latest is not None
        and latest.action == "grant"
        and latest.consent_text_version == ai_consent.current_version()
    )


def ai_policy_state() -> dict:
    """CONTRACT used by apps.ai's policy gate. Inside a tenant context.

    Returns {"policy": AIDataPolicy value, "consent_valid": bool}. A policy above synthetic_only is only
    effective with a valid consent (latest ledger row for that policy is a grant for the CURRENT consent
    text version, and the Gemini tier allows the policy); otherwise callers must treat the org as
    synthetic_only.
    """
    org = get_current_org()
    policy = org.ai_data_policy
    return {"policy": policy, "consent_valid": consent_is_valid(policy)}


def effective_ai_policy(org: Organization | None = None) -> str:
    org = org or get_current_org()
    return org.ai_data_policy if consent_is_valid(org.ai_data_policy) else SYNTHETIC_ONLY


def ai_policy_payload() -> dict:
    """Body of GET /orgs/{org}/ai-policy (CONTRACT shape + `configured_policy`)."""
    from apps.accounts.services import users_by_id

    org = get_current_org()
    configured = org.ai_data_policy
    valid = consent_is_valid(configured)
    consent = None
    if configured != SYNTHETIC_ONLY:
        latest = latest_consent(configured)
        grantor = None
        if latest is not None and latest.action == "grant":
            grantor = users_by_id([latest.actor_id]).get(str(latest.actor_id))
        consent = {
            "valid": valid,
            "version": latest.consent_text_version if latest else None,
            "granted_by": grantor.email if grantor else None,
            "granted_at": latest.created_at if latest is not None and latest.action == "grant" else None,
        }
    return {
        "policy": configured if valid else SYNTHETIC_ONLY,
        "configured_policy": configured,
        "gemini_tier": settings.GEMINI_TIER,
        "consent": consent,
        "allowed_values": allowed_policy_values(),
        "consent_text_version": ai_consent.current_version(),
    }


def _consent_row(action: str, policy: str, version: str, actor_id, request=None) -> AIPolicyConsent:
    ip = ua = None
    if request is not None:
        ip = request.META.get("REMOTE_ADDR") or None
        ua = (request.META.get("HTTP_USER_AGENT") or "")[:300]
    return AIPolicyConsent.objects.create(
        action=action,
        policy=policy,
        consent_text_version=version,
        gemini_tier=settings.GEMINI_TIER if settings.GEMINI_TIER in ("free", "paid", "vertex") else "free",
        actor_id=actor_id,
        ip=ip,
        user_agent=ua or "",
    )


def grant_ai_consent(*, policy: str, consent_text_version: str, acknowledged: bool, actor, request=None) -> dict:
    """Record a consent grant and switch the org's policy. The caller has already re-authenticated
    the actor (password) and checked `org.ai_data_policy.consent`."""
    if policy not in POLICY_ORDER or policy == SYNTHETIC_ONLY:
        raise ValidationError({"policy": ["Choose schema_only, aggregates_only or full (use revoke for synthetic_only)."]})
    if not policy_allowed_for_tier(policy):
        raise AIPolicyTierForbidden()
    if acknowledged is not True or str(consent_text_version) != ai_consent.current_version():
        raise AIPolicyConsentRequired(
            "You must acknowledge the current consent text "
            f"(version {ai_consent.current_version()}).",
            extra={"consent_text_version": ai_consent.current_version()},
        )
    org = Organization.objects.select_for_update().get()
    previous = org.ai_data_policy
    version = ai_consent.current_version()
    if previous not in (SYNTHETIC_ONLY, policy):
        _consent_row("revoke", previous, version, actor.pk, request)
    row = _consent_row("grant", policy, version, actor.pk, request)
    org.ai_data_policy = policy
    org.save(update_fields=["ai_data_policy", "updated_at"])
    audit.record(
        "ai.consent.grant",
        target=org,
        changes={"ai_data_policy": [previous, policy]},
        metadata={"consent_id": str(row.id), "policy": policy, "consent_text_version": version,
                  "gemini_tier": row.gemini_tier},
        request=request,
    )
    return ai_policy_payload()


def revoke_ai_consent(*, actor, request=None) -> dict:
    """Immediately revert the org to synthetic_only (idempotent)."""
    org = Organization.objects.select_for_update().get()
    previous = org.ai_data_policy
    if previous != SYNTHETIC_ONLY:
        row = _consent_row("revoke", previous, ai_consent.current_version(), actor.pk, request)
        org.ai_data_policy = SYNTHETIC_ONLY
        org.save(update_fields=["ai_data_policy", "updated_at"])
        audit.record(
            "ai.consent.revoke",
            target=org,
            changes={"ai_data_policy": [previous, SYNTHETIC_ONLY]},
            metadata={"consent_id": str(row.id), "policy": previous},
            request=request,
        )
    return ai_policy_payload()


# --- members -----------------------------------------------------------------------------------------
def member_payload(m: Membership) -> dict:
    return {
        "id": str(m.id),
        "user": {"id": str(m.user.pk), "email": m.user.email, "full_name": m.user.full_name},
        "role": m.role.key,
        "role_name": m.role.name,
        "status": m.status,
        "joined_at": m.joined_at,
    }


def _other_active_owner_exists(exclude_membership_id) -> bool:
    owners = list(
        Membership.objects.select_for_update(of=("self",))
        .filter(role__key=OWNER, status="active")
        .values_list("id", flat=True)
    )
    return any(mid != exclude_membership_id for mid in owners)


def _require_owner_for(actor_info: TenantInfo, *roles: str) -> None:
    if OWNER in roles and actor_info.role_key != OWNER:
        raise Forbidden("Only an owner can grant, change or remove the owner role.", code="owner_required")


def get_role(role_key: str, field: str = "role") -> Role:
    role = Role.objects.filter(key=role_key).first()
    if role is None:
        raise ValidationError({field: [f"Unknown role '{role_key}'."]})
    return role


def change_member(membership: Membership, *, actor_info: TenantInfo, role_key: str | None = None,
                  status: str | None = None, request=None) -> Membership:
    old_role = membership.role.key
    new_role = get_role(role_key) if role_key else membership.role
    _require_owner_for(actor_info, old_role, new_role.key)
    new_status = status or membership.status
    if new_status not in ("active", "suspended"):
        raise ValidationError({"status": ["Must be active or suspended."]})
    loses_owner = old_role == OWNER and membership.status == "active" and (
        new_role.key != OWNER or new_status != "active"
    )
    if loses_owner and not _other_active_owner_exists(membership.id):
        raise Conflict("The organization must keep at least one active owner.", code="last_owner")
    changes = {}
    if new_role.id != membership.role_id:
        changes["role"] = [old_role, new_role.key]
        membership.role = new_role
    if new_status != membership.status:
        changes["status"] = [membership.status, new_status]
        membership.status = new_status
    if changes:
        membership.save(update_fields=["role", "status", "updated_at"])
        audit.record("member.update", target=membership, changes=changes,
                     metadata={"user_id": str(membership.user_id)}, request=request)
    return membership


def remove_member(membership: Membership, *, actor_info: TenantInfo, request=None) -> None:
    role = membership.role.key
    _require_owner_for(actor_info, role)
    if role == OWNER and membership.status == "active" and not _other_active_owner_exists(membership.id):
        raise Conflict("The organization must keep at least one active owner.", code="last_owner")
    audit.record("member.remove", target=membership,
                 metadata={"user_id": str(membership.user_id), "email": membership.user.email, "role": role},
                 request=request)
    membership.delete()


# --- invitations -------------------------------------------------------------------------------------
def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def invitation_payload(inv: Invitation) -> dict:
    return {
        "id": str(inv.id),
        "email": inv.email,
        "role": inv.role.key,
        "expires_at": inv.expires_at,
        "status": inv.status,
        "created_at": inv.created_at,
    }


def _frontend_base_url() -> str:
    base = getattr(settings, "FRONTEND_BASE_URL", "") or (settings.CSRF_TRUSTED_ORIGINS or ["http://localhost:3000"])[0]
    return base.rstrip("/")


def invitation_link(token: str) -> str:
    return f"{_frontend_base_url()}/invitations/accept?token={token}"


def send_invitation_email(*, inv: Invitation, token: str, org: Organization, inviter) -> bool:
    inviter_name = (inviter.full_name or inviter.email) if inviter else "A teammate"
    subject = f"You're invited to join {org.name} on Cortix"
    body = (
        f"{inviter_name} invited you to join the organization \"{org.name}\" on Cortix "
        f"as {inv.role.name}.\n\n"
        f"Accept the invitation: {invitation_link(token)}\n\n"
        f"This link expires on {inv.expires_at:%Y-%m-%d %H:%M} UTC. If you did not expect this email, ignore it."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [inv.email], fail_silently=False)
        return True
    except Exception:  # noqa: BLE001 - the invitation stays valid; the inviter can re-send
        log.exception("cortix: sending invitation email failed (invitation %s)", inv.id)
        return False


def create_invitation(*, email: str, role_key: str, inviter, inviter_info: TenantInfo, request=None):
    """Create (or refresh) a pending invitation. Returns (invitation, token, email_sent)."""
    email = (email or "").strip().lower()
    role = get_role(role_key)
    _require_owner_for(inviter_info, role.key)
    if Membership.objects.filter(user__email__iexact=email).exists():
        raise Conflict("This user is already a member of the organization.", code="already_member")
    token = secrets.token_urlsafe(32)
    expires = timezone.now() + INVITATION_TTL
    inv = (
        Invitation.objects.select_for_update()
        .filter(email=email, accepted_at__isnull=True, revoked_at__isnull=True)
        .first()
    )
    action = "invitation.create"
    if inv is not None:
        inv.token_hash = hash_token(token)
        inv.expires_at = expires
        inv.role = role
        inv.invited_by = inviter.pk
        inv.save(update_fields=["token_hash", "expires_at", "role", "invited_by", "updated_at"])
        action = "invitation.resend"
    else:
        inv = Invitation.objects.create(
            email=email, role=role, token_hash=hash_token(token), expires_at=expires, invited_by=inviter.pk
        )
    sent = send_invitation_email(inv=inv, token=token, org=get_current_org(), inviter=inviter)
    audit.record(action, target=inv, metadata={"email": email, "role": role.key, "email_sent": sent},
                 request=request)
    return inv, token, sent


def revoke_invitation(inv: Invitation, *, request=None) -> None:
    if inv.accepted_at is None and inv.revoked_at is None:
        inv.revoked_at = timezone.now()
        inv.save(update_fields=["revoked_at", "updated_at"])
        audit.record("invitation.revoke", target=inv, metadata={"email": inv.email}, request=request)


@dataclass(frozen=True)
class InvitationRecord:
    id: UUID
    org_id: UUID
    org_slug: str
    email: str
    role_id: UUID
    expires_at: datetime


class InvitationInvalid(NexoraError):
    status_code = 400
    default_code = "invitation_invalid"
    default_detail = "This invitation is invalid, expired or has already been used."


def lookup_invitation(token: str) -> InvitationRecord | None:
    """Pre-tenant lookup by token hash (SECURITY DEFINER `nexora_invitation_by_token`)."""
    if not token or len(token) > 200:
        return None
    with connection.cursor() as cur:
        cur.execute(
            "SELECT invitation_id, org_id, org_slug, email, role_id, expires_at FROM nexora_invitation_by_token(%s)",
            [hash_token(token)],
        )
        row = cur.fetchone()
    if not row:
        return None
    return InvitationRecord(id=row[0], org_id=row[1], org_slug=row[2], email=row[3], role_id=row[4], expires_at=row[5])


def members_with_permission(codename: str) -> list[UUID]:
    """User ids of the ACTIVE members whose role grants `codename` (inside the org's tenant context).
    Registered with apps.core.notifications so lower layers can notify e.g. all org admins."""
    return list(
        Membership.objects.filter(status="active", role__role_permissions__permission__codename=codename)
        .values_list("user_id", flat=True)
        .distinct()
    )


def member_permissions(user_id) -> TenantInfo | None:
    """The CURRENT role and permissions of an active member of the active org (inside its tenant context), or None
    when the user is not an active member any more (or the account is inactive). Background work that acts for a
    user (automation runs, agents) re-checks this before every step, so a removed or demoted member's runs cannot
    keep their old privileges."""
    membership = (Membership.objects.select_related("role", "user")
                  .filter(user_id=user_id, status="active", user__is_active=True).first())
    if membership is None:
        return None
    perms = frozenset(RolePermission.objects.filter(role_id=membership.role_id)
                      .values_list("permission__codename", flat=True))
    org = Organization.objects.filter(id=membership.org_id).first()
    return TenantInfo(org_id=membership.org_id, slug=org.slug if org else "", role_key=membership.role.key,
                      permissions=perms)


def _notify_invitation_accepted(inv: Invitation, user) -> None:
    """The inviter and the people who manage members learn that someone joined (never the joiner)."""
    from apps.core.notifications import notify

    recipients = {inv.invited_by, *members_with_permission("members.manage")} - {user.pk}
    who = user.full_name or user.email
    notify(sorted(recipients, key=str), "invitation.accepted", f"{who} joined the organization",
           f"{user.email} accepted the invitation as {inv.role.name}.", "/settings/members")


def accept_invitation(*, record: InvitationRecord, user, request=None) -> dict:
    """Join the invited org (idempotent for an existing membership). Returns the Org payload."""
    from apps.core.tenancy.views import resolve_membership

    if user.email.lower() != record.email.lower():
        raise Forbidden("This invitation was sent to a different email address.", code="invitation_email_mismatch")
    with tenant_context(record.org_id):
        inv = Invitation.objects.select_for_update().filter(id=record.id).first()
        if inv is None or inv.status != "pending":
            raise InvitationInvalid()
        membership = Membership.objects.filter(user=user).first()
        if membership is None:
            membership = Membership.objects.create(user=user, role_id=inv.role_id, invited_by=inv.invited_by)
        inv.accepted_at = timezone.now()
        inv.accepted_by = user.pk
        inv.save(update_fields=["accepted_at", "accepted_by", "updated_at"])
        audit.record("invitation.accept", target=inv,
                     metadata={"email": inv.email, "membership_id": str(membership.id)},
                     actor=("user", user.pk), request=request)
        _notify_invitation_accepted(inv, user)
        info = resolve_membership(user.pk, record.org_slug)
        if info is None:  # membership exists but is suspended
            raise Forbidden("Your membership in this organization is suspended.", code="membership_suspended")
        return org_payload(get_current_org(), info)
