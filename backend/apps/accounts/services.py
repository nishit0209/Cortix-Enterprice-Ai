"""Public service API of apps.accounts. Other apps import only from here."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction

from apps.orgs.permissions_catalog import PERMISSIONS

API_KEY_PREFIX = "nxk"
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
PREFIX_LEN = 8

# Codenames an API key can never carry: people/role/key management, AI consent and governance edits are
# interactive-user-only (API_SPECIFICATION.md §1 "Authentication").
API_KEY_FORBIDDEN_SCOPES = frozenset(
    {
        "org.settings.edit",
        "org.ai_policy.consent",
        "members.manage",
        "roles.manage",
        "api_keys.manage",
        "governance.policy.edit",
        "governance.retention.edit",
        "approvals.decide",
        "ml.promote",  # W4: promoting a model to production is an interactive, audited decision
    }
)
API_KEY_ALLOWED_SCOPES = frozenset(PERMISSIONS) - API_KEY_FORBIDDEN_SCOPES


# --- users -------------------------------------------------------------------------------------------
def user_payload(user) -> dict:
    return {
        "id": str(user.pk),
        "email": user.email,
        "full_name": user.full_name,
        "is_platform_admin": bool(user.is_platform_admin),
        "timezone": user.timezone,
        "mfa_enabled": bool(user.mfa_enabled),
    }


def set_platform_admin(user, flag: bool) -> bool:
    """Set `User.is_platform_admin`; returns whether it changed (callers audit it)."""
    flag = bool(flag)
    if bool(user.is_platform_admin) == flag:
        return False
    user.is_platform_admin = flag
    user.save(update_fields=["is_platform_admin"])
    return True


def find_user_by_email(email: str):
    from .models import User

    return User.objects.filter(email__iexact=(email or "").strip()).first()


def ensure_user(email: str, *, password: str | None = None, full_name: str = ""):
    """Get or create a user by email (idempotent; used by `manage.py seed_demo`). Returns (user, created).

    A given `password` is validated with the configured password validators and set (also on an existing user),
    except when the existing user's password already equals it: then the stored hash is NOT re-hashed (a new salted
    hash would needlessly invalidate the user's sessions). Without one an existing user's password is left untouched
    and a new user gets an unusable password. `user.password_changed` tells the caller whether it was (re)set."""
    from django.contrib.auth.password_validation import validate_password

    from .models import User

    email = (email or "").strip().lower()
    user = find_user_by_email(email)
    created = user is None
    if created:
        user = User(email=email, full_name=(full_name or "")[:150])
    changed = False
    if password:
        validate_password(password, user)
        if created or not user.check_password(password):
            user.set_password(password)
            changed = True
    elif created:
        user.set_unusable_password()
    if created or changed:
        user.save()
    user.password_changed = changed
    return user, created


def users_by_id(ids) -> dict:
    """Bulk-load global users (no tenant data) keyed by id string."""
    from .models import User

    clean = {str(i) for i in ids if i}
    if not clean:
        return {}
    return {str(u.pk): u for u in User.objects.filter(id__in=clean)}


# --- API keys ----------------------------------------------------------------------------------------
def hash_api_secret(secret: str) -> bytes:
    return hmac.new(settings.API_KEY_PEPPER.encode(), secret.encode(), hashlib.sha256).digest()


def _new_prefix() -> str:
    return "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LEN))


def format_api_key(prefix: str, secret: str) -> str:
    return f"{API_KEY_PREFIX}_{prefix}_{secret}"


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """'nxk_<prefix>_<secret>' -> (prefix, secret), or None when it is not shaped like a Nexora key."""
    parts = (raw or "").split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX:
        return None
    prefix, secret = parts[1], parts[2]
    if len(prefix) != PREFIX_LEN or any(c not in _PREFIX_ALPHABET for c in prefix) or len(secret) < 32:
        return None
    return prefix, secret


def create_api_key(*, name: str, scopes: list[str], created_by, expires_at: datetime | None = None):
    """Inside a tenant context. Returns (ApiKey, full_token). The token is never stored."""
    from .models import ApiKey

    secret = secrets.token_urlsafe(32)  # 256-bit secret
    for _ in range(5):
        prefix = _new_prefix()
        try:
            with transaction.atomic():
                key = ApiKey.objects.create(
                    name=name.strip()[:100],
                    prefix=prefix,
                    key_hash=hash_api_secret(secret),
                    scopes=sorted(set(scopes)),
                    created_by=created_by,
                    expires_at=expires_at,
                )
            return key, format_api_key(prefix, secret)
        except IntegrityError:  # prefix collision (globally unique) -> retry with a new prefix
            continue
    raise RuntimeError("could not allocate a unique API key prefix")


@dataclass(frozen=True)
class ApiKeyRecord:
    id: UUID
    org_id: UUID
    org_slug: str
    key_hash: bytes
    scopes: tuple[str, ...]
    expires_at: datetime | None
    revoked_at: datetime | None
    created_by: UUID
    creator_permissions: frozenset[str]
    last_used_at: datetime | None
    org_status: str = "active"


def lookup_api_key(prefix: str) -> ApiKeyRecord | None:
    """Pre-tenant lookup through the SECURITY DEFINER function `nexora_api_key_lookup`.

    Only returns keys whose org is active or suspended (`org_status`; authentication answers 403 `org_suspended`
    for the latter) and whose creator is still an active member of that org.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT api_key_id, org_id, org_slug, key_hash, scopes, expires_at, revoked_at, created_by, "
            "creator_permissions, last_used_at, org_status FROM nexora_api_key_lookup(%s)",
            [prefix],
        )
        row = cur.fetchone()
    if not row:
        return None
    return ApiKeyRecord(
        id=row[0],
        org_id=row[1],
        org_slug=row[2],
        key_hash=bytes(row[3]),
        scopes=tuple(row[4] or ()),
        expires_at=row[5],
        revoked_at=row[6],
        created_by=row[7],
        creator_permissions=frozenset(row[8] or ()),
        last_used_at=row[9],
        org_status=row[10] or "active",
    )


def verify_api_secret(record: ApiKeyRecord, secret: str) -> bool:
    return hmac.compare_digest(record.key_hash, hash_api_secret(secret))


# --- login protection (5 failures / 15 min per account+IP -> exponential backoff) --------------------
LOGIN_FAILURE_THRESHOLD = 5
LOGIN_FAILURE_WINDOW_S = 15 * 60
LOGIN_MAX_LOCK_S = 15 * 60


def _login_key(email: str, ip: str) -> str:
    digest = hashlib.sha256(f"{(email or '').strip().lower()}|{ip or ''}".encode()).hexdigest()
    return f"nexora:login_fail:{digest}"


def login_lock_remaining(email: str, ip: str) -> int:
    state = cache.get(_login_key(email, ip)) or {}
    remaining = int(state.get("locked_until", 0) - time.time())
    return max(remaining, 0)


def register_login_failure(email: str, ip: str) -> int:
    """Record a failure; returns the lock duration in seconds (0 if not locked)."""
    key = _login_key(email, ip)
    state = cache.get(key) or {"count": 0, "locked_until": 0}
    state["count"] = int(state.get("count", 0)) + 1
    lock = 0
    if state["count"] >= LOGIN_FAILURE_THRESHOLD:
        lock = min(30 * 2 ** (state["count"] - LOGIN_FAILURE_THRESHOLD), LOGIN_MAX_LOCK_S)
        state["locked_until"] = time.time() + lock
    cache.set(key, state, LOGIN_FAILURE_WINDOW_S + lock)
    return lock


def reset_login_failures(email: str, ip: str) -> None:
    cache.delete(_login_key(email, ip))


def client_ip(request) -> str:
    return (request.META.get("REMOTE_ADDR") or "")[:45]
