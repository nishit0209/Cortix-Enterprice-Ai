"""Isolation-matrix factories for orgs/accounts tenant routes (see backend/testing/isolation.py).

Each factory creates a REAL resource in `world`'s org and returns the URL kwargs that address it."""

from __future__ import annotations

import secrets
from datetime import timedelta


def _member(world) -> dict[str, str]:
    from apps.orgs.models import Membership

    with world.tenant():
        m = Membership.objects.get(user=world.analyst)
    world.resources["membership"] = m
    return {"pk": str(m.id)}


def _invitation(world) -> dict[str, str]:
    from django.utils import timezone

    from apps.orgs.models import Invitation, Role
    from apps.orgs.services import hash_token

    inv = world.resources.get("invitation")
    if inv is None:
        with world.tenant():
            inv = Invitation.objects.create(
                email=f"isolation-invitee-{secrets.token_hex(4)}@example.test",
                role=Role.objects.get(key="viewer"),
                token_hash=hash_token(secrets.token_urlsafe(32)),
                expires_at=timezone.now() + timedelta(days=7),
                invited_by=world.owner.pk,
            )
        world.resources["invitation"] = inv
    return {"pk": str(inv.id)}


def _api_key(world) -> dict[str, str]:
    from apps.accounts.services import create_api_key

    key = world.resources.get("api_key")
    if key is None:
        with world.tenant():
            key, _token = create_api_key(name="isolation-probe", scopes=["org.view"], created_by=world.owner)
        world.resources["api_key"] = key
    return {"pk": str(key.id)}


ISOLATION_FACTORIES = {
    "member-detail": _member,
    "invitation-detail": _invitation,
    "api-key-detail": _api_key,
}
