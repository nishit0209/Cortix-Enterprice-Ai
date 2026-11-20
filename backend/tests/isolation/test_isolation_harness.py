"""Self-tests of the isolation-matrix harness (testing.routes / testing.isolation). No database needed."""

from __future__ import annotations

import types

import pytest

from testing import isolation
from testing.factories import OrgWorld
from testing.isolation import (
    IsolationFactoryError,
    IsolationRegistryConflict,
    MissingIsolationFixture,
    factory_for,
    once,
    owning_app,
)
from testing.routes import Route, Segment


def _route(name: str, pattern: str, kwargs: tuple[str, ...], module: str = "apps.bi.views") -> Route:
    view_cls = type("ConversationViewSet", (), {"__module__": module, "get": lambda self: None})
    callback = types.SimpleNamespace(cls=view_cls, actions={"get": "retrieve", "delete": "destroy"})
    converters = tuple((k, "UUIDConverter") for k in ("org", *kwargs))
    return Route(name=name, segments=(Segment("route", pattern, converters),), callback=callback)


def test_missing_fixture_message_names_owner_file_route_and_snippet():
    route = _route(
        "qa-probe-conversation-detail",
        "api/v1/orgs/<slug:org>/bi/conversations/<uuid:conversation_id>/qa-probe",
        ("conversation_id",),
    )
    with pytest.raises(MissingIsolationFixture) as exc:
        factory_for(route)
    msg = str(exc.value)
    assert "'qa-probe-conversation-detail'" in msg
    assert "bi/conversations/<uuid:conversation_id>/qa-probe" in msg
    assert "apps.bi.views.ConversationViewSet" in msg
    assert "GET, DELETE" in msg
    assert "backend/apps/bi/tests/isolation.py" in msg
    assert "['conversation_id']" in msg
    assert 'ISOLATION_FACTORIES = {"qa-probe-conversation-detail": _factory}' in msg
    assert 'KWARG_FACTORIES = {"conversation_id": ...}' in msg


def test_owning_app_is_derived_from_the_view_module():
    assert owning_app(_route("x", "p", (), module="apps.pipelines.views")) == "pipelines"
    assert owning_app(_route("x", "p", (), module="thirdparty.views")) is None


def _world() -> OrgWorld:
    return OrgWorld(org=types.SimpleNamespace(id="o", slug="s", short_id="x"))


def test_factory_with_wrong_kwargs_is_reported_precisely(monkeypatch):
    route = _route("qa-probe-run", "api/v1/orgs/<slug:org>/pipeline-runs/<uuid:run_id>", ("run_id",))
    monkeypatch.setattr(isolation, "registry", lambda: ({"qa-probe-run": lambda w: {"pk": "1"}}, {}, {}))
    factory = factory_for(route)
    with pytest.raises(IsolationFactoryError) as exc:
        factory(_world())
    assert "needs non-empty values for ['run_id']" in str(exc.value)
    assert "'qa-probe-run'" in str(exc.value)


def test_factory_returning_non_dict_is_reported(monkeypatch):
    route = _route("qa-probe-run", "api/v1/orgs/<slug:org>/pipeline-runs/<uuid:run_id>", ("run_id",))
    monkeypatch.setattr(isolation, "registry", lambda: ({"qa-probe-run": lambda w: "abc"}, {}, {}))
    with pytest.raises(IsolationFactoryError, match="returned str"):
        factory_for(route)(_world())


def test_kwarg_factories_are_combined_and_exempt_routes_return_none(monkeypatch):
    route = _route(
        "qa-probe-step",
        "api/v1/orgs/<slug:org>/p/<uuid:pipeline_id>/r/<uuid:run_id>",
        ("pipeline_id", "run_id"),
    )
    kwargs = {"pipeline_id": lambda w: {"pipeline_id": "p1"}, "run_id": lambda w: {"run_id": "r1"}}
    monkeypatch.setattr(isolation, "registry", lambda: ({}, kwargs, {}))
    assert factory_for(route)(_world()) == {"pipeline_id": "p1", "run_id": "r1"}
    monkeypatch.setattr(isolation, "registry", lambda: ({}, {}, {"qa-probe-step": "no resource"}))
    assert factory_for(route) is None


def test_duplicate_registration_across_apps_is_a_conflict():
    target, origins = {"x": 1}, {"x": "apps.a.tests.isolation"}
    with pytest.raises(IsolationRegistryConflict, match="registered by both"):
        isolation._merge(target, origins, {"x": 2}, "apps.b.tests.isolation", "ISOLATION_FACTORIES")


def test_once_builds_a_resource_once_per_world():
    world, calls = _world(), []
    first = once(world, "k", lambda: calls.append(1) or object())
    assert once(world, "k", lambda: calls.append(1) or object()) is first
    assert calls == [1]


def test_registry_loads_without_errors():
    routes, kwargs, exempt = isolation.registry()
    assert "job-detail" in routes and "job_id" in kwargs
    assert all(str(reason).strip() for reason in exempt.values())
