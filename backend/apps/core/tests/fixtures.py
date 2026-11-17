"""Shared factories/fixtures for the Backend agent's app tests (synthetic data only).

Import into an app's tests/conftest.py:  from apps.core.tests.fixtures import *  # noqa: F401,F403
"""

from __future__ import annotations

import uuid

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.core.tenancy import tenant_context

DEFAULT_PASSWORD = "Synthetic-Passw0rd-2026!"


def unique_email(label: str = "user") -> str:
    return f"{label}.{uuid.uuid4().hex[:10]}@example.test"


def make_user(email: str | None = None, password: str = DEFAULT_PASSWORD, full_name: str = "Synthetic User", **extra):
    from apps.accounts.models import User

    return User.objects.create_user(email or unique_email(), password, full_name=full_name, **extra)


def make_org(owner=None, name: str = "Synthetic Org"):
    from apps.orgs.services import create_organization

    owner = owner or make_user(full_name="Synthetic Owner")
    return create_organization(name=name, owner=owner)


def add_member(org, user, role_key: str, status: str = "active"):
    from apps.orgs.models import Membership, Role

    with tenant_context(org.id):
        role = Role.objects.get(key=role_key)
        return Membership.objects.create(user=user, role=role, status=status)


def member_of(org, role_key: str, **kwargs):
    user = make_user(**kwargs)
    add_member(org, user, role_key)
    return user


def client_for(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_login(user)
    return client


def org_url(org, path: str = "") -> str:
    return f"/api/v1/orgs/{org.slug}/{path}" if path else f"/api/v1/orgs/{org.slug}"


@pytest.fixture(autouse=True)
def clear_cache_between_tests():  # public name: `import *` must re-export it
    """Throttle counters and login lockouts live in the cache: isolate every test."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def owner(db):
    return make_user(full_name="Olivia Owner")


@pytest.fixture
def org(owner):
    return make_org(owner, name="Acme Synthetic Analytics")


@pytest.fixture
def other_org(db):
    return make_org(make_user(full_name="Bruno Other"), name="Beta Synthetic Retail")


@pytest.fixture
def owner_client(owner):
    return client_for(owner)


@pytest.fixture
def anon_client():
    return APIClient()
