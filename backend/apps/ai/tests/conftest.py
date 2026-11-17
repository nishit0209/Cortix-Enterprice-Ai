"""Shared fixtures for apps.ai tests. No test here calls Gemini: transports are FakeTransport."""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.core.tenancy.context import current_actor, current_org


@pytest.fixture
def org_ctx():
    """A tenant context WITHOUT a database transaction (pure unit tests of the engine)."""
    org_id, user_id = uuid4(), uuid4()
    t1 = current_org.set(org_id)
    t2 = current_actor.set(("user", user_id))
    try:
        yield org_id, user_id
    finally:
        current_actor.reset(t2)
        current_org.reset(t1)


@pytest.fixture
def gemini_key(settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL-0000"
    settings.GEMINI_TIER = "free"
    settings.GEMINI_MODEL_FAST = "gemini-3.5-flash-lite"
    settings.GEMINI_MODEL_REASONING = "gemini-3.8-flash"
    return settings.GEMINI_API_KEY
