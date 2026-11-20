"""Shared pytest fixtures (QA/Security-owned). Loaded globally via backend/conftest.py `pytest_plugins`.

Fixtures (all synthetic data):
    make_user(email=None, password=TEST_PASSWORD, **extra)     -> User           (needs db)
    make_org(name=None, owner=None, slug=None)                 -> Organization   (real create_organization)
    org_world(name, roles=("analyst","viewer"))                -> OrgWorld
    two_orgs                                                   -> TwoOrgs(a, b, outsider); each org has
                                                                  owner + analyst + viewer
    api_client_for(user=None, csrf=True)                       -> CsrfAPIClient (CSRF ENFORCED, session login)
    anon_client                                                -> CsrfAPIClient, not logged in
    tenant(org | world | uuid)                                 -> context manager: tenant_context + GUC cleanup
    fake_gemini                                                -> FakeGeminiService installed as the Gemini singleton
    gated_fake_gemini                                          -> same, behind the REAL AIDataPolicy gate
    gated_gemini_engine                                        -> real GeminiEngine on AI/ML's FakeTransport
    memory_storage                                             -> MemoryObjectStorage patched into apps.core.storage
    fake_embedder                                              -> deterministic FakeEmbedder (no model download)
    network_spy                                                -> NetworkSpy (blocks non-local connections)
    fake_redis                                                 -> FakeRedis patched into apps.core.jobs
    fake_clock                                                 -> FakeClock patched into timezone.now
    canaries                                                   -> Canaries (fake secrets that must never leak)

Fixtures defined locally in an app's tests override these by name (pytest semantics).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture
def make_user(db):
    from .factories import make_user as _make_user

    return _make_user


@pytest.fixture
def make_org(db):
    from .factories import make_org as _make_org

    return _make_org


@pytest.fixture
def org_world(db):
    from .factories import make_org_world

    return make_org_world


@pytest.fixture
def two_orgs(db):
    from .db import clear_db_tenant
    from .factories import TwoOrgs, make_org_world, make_user

    a = make_org_world("Acme Analytics", slug="acme-analytics")
    b = make_org_world("Beta Retail", slug="beta-retail")
    outsider = make_user("outsider@example.test")
    clear_db_tenant()
    return TwoOrgs(a=a, b=b, outsider=outsider)


@pytest.fixture
def api_client_for(db):
    from .client import api_client_for as _client_for

    return _client_for


@pytest.fixture
def anon_client(db):
    from .client import api_client_for as _client_for

    return _client_for(None)


@pytest.fixture
def tenant(db):
    """`with tenant(org_a): ...` — accepts an Organization, an OrgWorld or an id."""
    from apps.core.tenancy import tenant_context

    from .db import clear_db_tenant

    @contextmanager
    def _tenant(target):
        org_id = getattr(target, "id", target)
        try:
            with tenant_context(org_id):
                yield
        finally:
            clear_db_tenant()

    return _tenant


def _install_gemini(monkeypatch, engine) -> None:
    """Make every `apps.ai.services.gemini()` caller (also `from apps.ai.services import gemini`) get `engine`."""
    import apps.ai.services as ai_services

    monkeypatch.setattr(ai_services, "_engine", engine)


@pytest.fixture
def fake_gemini(monkeypatch):
    """Scripted FakeGeminiService installed as THE Gemini singleton. Never touches the network."""
    from .fakes import FakeGeminiService

    fake = FakeGeminiService()
    _install_gemini(monkeypatch, fake)
    return fake


@pytest.fixture
def gated_fake_gemini(monkeypatch):
    """FakeGeminiService behind the REAL AIDataPolicy gate (apps.ai.services.check_policy).

    `.calls` holds only prompts the gate let through; blocked attempts are in `.blocked`."""
    from apps.ai.services import check_policy

    from .fakes import FakeGeminiService

    fake = FakeGeminiService(policy_check=check_policy)
    _install_gemini(monkeypatch, fake)
    return fake


@pytest.fixture
def gated_gemini_engine(monkeypatch):
    """The REAL GeminiEngine (policy gate, redaction, quota, request log) on AI/ML's FakeTransport.

    Returns (engine, parts): parts["transport"].calls / .stream_calls are the requests that would have reached
    Google; parts["recorder"].records are the request-log records (incl. blocked_by_policy)."""
    from apps.ai.services import check_policy
    from apps.ai.testing import build_engine

    engine, parts = build_engine(policy_check=check_policy)
    _install_gemini(monkeypatch, engine)
    return engine, parts


@pytest.fixture
def fake_embedder(monkeypatch):
    """AI/ML's deterministic FakeEmbedder as the process embedder (no model download, no network)."""
    from apps.ai import embeddings

    fake = embeddings.FakeEmbedder()
    monkeypatch.setattr(embeddings, "_embedder", fake)
    return fake


@pytest.fixture
def memory_storage(monkeypatch):
    """In-memory object storage patched into apps.core.storage (still enforces the orgs/<org>/ prefix)."""
    from .storage import MemoryObjectStorage

    return MemoryObjectStorage().install(monkeypatch)


@pytest.fixture
def network_spy():
    from .fakes import NetworkSpy

    with NetworkSpy() as spy:
        yield spy


@pytest.fixture
def fake_redis(monkeypatch, settings):
    """FakeRedis behind apps.core.jobs, and JOB_EVENTS_BACKEND="redis" (the test settings default is "db")."""
    from apps.core import jobs

    settings.JOB_EVENTS_BACKEND = "redis"

    from .fakes import FakeRedis

    fake = FakeRedis()
    monkeypatch.setattr(jobs, "_redis_client", fake)
    monkeypatch.setattr(jobs, "redis_client", lambda: fake)
    return fake


@pytest.fixture
def fake_clock(monkeypatch):
    from .fakes import FakeClock

    return FakeClock().install(monkeypatch)


@pytest.fixture
def canaries():
    from .security import Canaries

    return Canaries()
