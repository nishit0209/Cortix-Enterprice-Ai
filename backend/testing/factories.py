"""Synthetic users, organizations and memberships built through the real services.

    user = make_user()                                  # unique synthetic email, TEST_PASSWORD
    world = make_org_world("Acme Analytics")            # owner + analyst + viewer (+ extra roles on demand)
    with world.tenant():
        ...

Orgs are created with `apps.orgs.services.create_organization` (the production path: seeded system roles,
owner membership, audit event, synthetic_only AI policy). Extra members are added with the ORM inside the
org's tenant context until the Backend agent ships an invitation/membership service.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# Synthetic credential used by every test user (generated for this test suite; not a real password).
TEST_PASSWORD = "Nx-synthetic-Pa55word!"
EMAIL_DOMAIN = "example.test"  # RFC 2606 reserved: never routable

_seq = itertools.count(1)


def _next(prefix: str) -> str:
    return f"{prefix}{next(_seq):04d}"


def make_user(
    email: str | None = None, *, password: str | None = TEST_PASSWORD, full_name: str = "", **extra
):
    from apps.accounts.models import User

    email = email or f"{_next('user')}@{EMAIL_DOMAIN}"
    return User.objects.create_user(
        email, password, full_name=full_name or email.split("@")[0].title(), **extra
    )


def make_org(name: str | None = None, *, owner=None, slug: str | None = None):
    """Create an org via the real service. The DB tenant setting is cleared afterwards (see testing.db)."""
    from apps.orgs.services import create_organization

    from .db import clear_db_tenant

    owner = owner or make_user()
    org = create_organization(name=name or _next("Synthetic Org "), owner=owner, slug=slug)
    clear_db_tenant()
    return org


def add_member(org, user, role_key: str, *, status: str = "active"):
    from apps.core.tenancy import tenant_context
    from apps.orgs.models import Membership, Role

    from .db import clear_db_tenant

    with tenant_context(org.id):
        role = Role.objects.get(key=role_key)
        membership = Membership.objects.create(org_id=org.id, user=user, role=role, status=status)
    clear_db_tenant()
    return membership


def make_job(org, *, user=None, type: str = "test.job", **fields):
    from apps.core.models import Job
    from apps.core.tenancy import tenant_context

    from .db import clear_db_tenant

    with tenant_context(org.id):
        job = Job.objects.create(type=type, user_id=getattr(user, "pk", user), **fields)
    clear_db_tenant()
    return job


def set_org_fields(org, **fields) -> None:
    """Update the organization row (inside its own tenant context; the org self-policy allows it)."""
    from apps.core.tenancy import tenant_context
    from apps.orgs.models import Organization

    from .db import clear_db_tenant

    with tenant_context(org.id):
        Organization.objects.filter(id=org.id).update(**fields)
    clear_db_tenant()
    for k, v in fields.items():
        setattr(org, k, v)


@dataclass
class OrgWorld:
    """One organization with a user per role. `users[role_key]` -> User."""

    org: Any
    users: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)  # free-form: tests stash created objects here

    @property
    def id(self) -> UUID:
        return self.org.id

    @property
    def slug(self) -> str:
        return self.org.slug

    @property
    def owner(self):
        return self.users["owner"]

    @property
    def analyst(self):
        return self.users["analyst"]

    @property
    def viewer(self):
        return self.users["viewer"]

    def user(self, role_key: str):
        """The member with `role_key`, created on first use (any system role, e.g. 'admin', 'auditor')."""
        if role_key not in self.users:
            u = make_user(f"{self.slug}-{role_key}-{next(_seq):04d}@{EMAIL_DOMAIN}")
            add_member(self.org, u, role_key)
            self.users[role_key] = u
        return self.users[role_key]

    @contextmanager
    def tenant(self) -> Iterator[None]:
        from apps.core.tenancy import tenant_context

        from .db import clear_db_tenant

        try:
            with tenant_context(self.org.id):
                yield
        finally:
            clear_db_tenant()

    def identifiers(self) -> set[str]:
        """Strings that must never appear in another org's responses."""
        ids = {str(self.org.id), self.org.slug, self.org.short_id}
        ids |= {str(u.pk) for u in self.users.values()}
        ids |= {u.email for u in self.users.values()}
        for value in self.resources.values():
            pk = getattr(value, "pk", None)
            if pk is not None:
                ids.add(str(pk))
        return ids


def make_org_world(
    name: str, *, slug: str | None = None, roles: tuple[str, ...] = ("analyst", "viewer")
) -> OrgWorld:
    tag = slug or _next("org")
    owner = make_user(f"{tag}-owner@{EMAIL_DOMAIN}", full_name=f"{name} Owner")
    org = make_org(name, owner=owner, slug=slug)
    world = OrgWorld(org=org, users={"owner": owner})
    for role_key in roles:
        world.user(role_key)
    return world


@dataclass
class TwoOrgs:
    a: OrgWorld
    b: OrgWorld
    outsider: Any  # a user with no membership anywhere

    def __iter__(self):  # allows `a, b = two_orgs`
        return iter((self.a, self.b))
