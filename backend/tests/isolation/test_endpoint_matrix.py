"""URLconf-driven tenant isolation matrix (TESTING_STRATEGY §4.1, MULTI_TENANCY_DESIGN §7.1).

Every route under /api/v1/orgs/<org>/ is discovered from the live URLconf at collection time, so endpoints that
other agents add later are covered automatically:

  1. authenticated non-members (each role of org A, and a user with no org) calling org B's slug get 404 on every
     method the view handles — with B's REAL resource ids in the path when an isolation factory exists;
  2. anonymous callers get 401 (CONTRACT "W1 as-built": existence is never revealed, no 2xx, no 404);
  3. IDOR: org B's owner calling B's own slug with org A's resource ids gets 404, and B's list endpoints never
     contain any of A's identifiers;
  4. every route with resource kwargs has an isolation factory (MissingIsolationFixture otherwise);
  5. unsafe methods without a CSRF token are refused for logged-in members.
"""

from __future__ import annotations

import pytest

from testing.client import api_client_for
from testing.isolation import MissingIsolationFixture, factory_for, stale_registrations
from testing.routes import SAFE_METHODS, UNSAFE_METHODS, Route, tenant_routes, verify_build

# DB access comes with the two_orgs fixture; discovery/registry tests run without a database.

ROUTES: list[Route] = tenant_routes()
NON_MEMBER_ACTORS = ("a_owner", "a_analyst", "a_viewer", "outsider")


def _route_methods(routes, methods):
    return [pytest.param(r, m, id=f"{r.id}-{m.upper()}") for r in routes for m in r.methods if m in methods]


def _actor(two_orgs, actor: str):
    if actor == "outsider":
        return two_orgs.outsider
    org, role = actor.split("_", 1)
    return getattr(two_orgs, org).users[role]


def _values(route: Route, world) -> dict[str, str]:
    try:
        factory = factory_for(route)
    except MissingIsolationFixture:
        return {}  # reported by test_every_resource_route_has_an_isolation_factory; random ids still probe the gate
    return factory(world) if factory else {}


def _call(client, method: str, path: str):
    if method in UNSAFE_METHODS:
        return getattr(client, method)(path, {}, format="json")
    return client.get(path)


def _body(resp) -> str:
    if getattr(resp, "streaming", False):
        pytest.fail(f"unexpected streaming response ({resp.status_code}) for an isolation probe")
    return resp.content.decode("utf-8", errors="replace")


def _leaks(text: str, identifiers: set[str]) -> list[str]:
    return sorted(i for i in identifiers if i and i in text)


# ------------------------------------------------------------------------------------------------ discovery
def test_tenant_routes_are_discovered_and_buildable():
    assert ROUTES, "no tenant routes discovered under /api/v1/orgs/<org>/"
    names = {r.name for r in ROUTES}
    assert {"job-list", "job-detail", "job-events", "audit-event-list"} <= names
    unbuildable = []
    for r in ROUTES:
        path = r.build(org="some-org")
        if not verify_build(r, path):
            unbuildable.append(f"{r.id}: {r.pattern} -> {path}")
    assert unbuildable == []


def test_every_resource_route_has_an_isolation_factory():
    missing = []
    for r in ROUTES:
        if r.resource_kwargs:
            try:
                factory_for(r)
            except MissingIsolationFixture as exc:
                missing.append(str(exc))
    assert missing == [], "\n".join(missing)


def test_isolation_registrations_match_live_routes():
    """A factory/exemption registered under a route name that does not exist is a typo or a renamed route."""
    assert stale_registrations({r.name for r in ROUTES if r.name}) == []


# W2/W3 contract routes (CONTRACT.md + API_SPECIFICATION Phase 3/4/8). Each must be discovered by the matrix and,
# when it has resource kwargs, have an isolation factory. A path that does not resolve yet is reported as a skip
# (the feature is not merged), never silently ignored.
_ID = "018f0000-0000-7000-8000-0000000000aa"
W2_CONTRACT_PATHS = [
    ("data-sources", "data-sources"),
    ("data-source", f"data-sources/{_ID}"),
    ("data-source-test", f"data-sources/{_ID}/test"),
    ("data-source-introspect", f"data-sources/{_ID}/introspect"),
    ("data-source-import", f"data-sources/{_ID}/import"),
    ("pipelines", "pipelines"),
    ("pipeline", f"pipelines/{_ID}"),
    ("pipeline-runs", f"pipelines/{_ID}/runs"),
    ("pipeline-run", f"pipeline-runs/{_ID}"),
    ("semantic-models", "semantic-models"),
    ("semantic-model", f"semantic-models/{_ID}"),
    ("bi-conversations", "bi/conversations"),
    ("bi-conversation", f"bi/conversations/{_ID}"),
    ("bi-messages", f"bi/conversations/{_ID}/messages"),
    ("bi-queries", "bi/queries"),
    ("bi-query-history", "bi/query-history"),
    ("exports", "exports"),
    ("export", f"exports/{_ID}"),
    ("documents", "documents"),
    ("document", f"documents/{_ID}"),
    ("documents-ask", "documents/ask"),
]


@pytest.mark.parametrize(("label", "suffix"), W2_CONTRACT_PATHS, ids=[p[0] for p in W2_CONTRACT_PATHS])
def test_w2_contract_routes_are_in_the_matrix(label, suffix):
    from django.urls import Resolver404, resolve

    path = f"/api/v1/orgs/some-org/{suffix}"
    try:
        match = resolve(path)
    except Resolver404:
        pytest.skip(f"{label}: {path} not routed yet (feature not merged)")
    route = next((r for r in ROUTES if verify_build(r, path)), None)
    assert route is not None, f"{path} resolves to {match.view_name} but is not discovered as a tenant route"
    if route.resource_kwargs:
        try:
            factory_for(route)
        except MissingIsolationFixture as exc:
            pytest.fail(str(exc))


def test_anonymous_tenant_route_is_401_not_404(two_orgs):
    """Contract: anonymous -> 401 on tenant routes, also for an org slug that does not exist."""
    assert api_client_for(None).get("/api/v1/orgs/no-such-org-qa/jobs").status_code == 401
    assert api_client_for(None).get(f"/api/v1/orgs/{two_orgs.b.slug}/jobs").status_code == 401


# ------------------------------------------------------------------------------------------------ 1. non-members
@pytest.mark.ac("P2-AC2")
@pytest.mark.parametrize("actor", NON_MEMBER_ACTORS)
@pytest.mark.parametrize(("route", "method"), _route_methods(ROUTES, SAFE_METHODS + UNSAFE_METHODS))
def test_non_member_gets_404(two_orgs, route, method, actor):
    b = two_orgs.b
    values = _values(route, b)  # B's real resources (falls back to random ids)
    path = route.build(org=b.slug, **values)
    client = api_client_for(_actor(two_orgs, actor))
    resp = _call(client, method, path)
    assert resp.status_code == 404, (
        f"{method.upper()} {path} as {actor} -> {resp.status_code}: {_body(resp)[:300]}"
    )
    assert _leaks(_body(resp), b.identifiers() | set(values.values())) == []


# ------------------------------------------------------------------------------------------------ 2. anonymous
@pytest.mark.parametrize(("route", "method"), _route_methods(ROUTES, SAFE_METHODS + UNSAFE_METHODS))
def test_anonymous_never_gets_data(two_orgs, route, method):
    b = two_orgs.b
    values = _values(route, b)
    path = route.build(org=b.slug, **values)
    resp = _call(api_client_for(None), method, path)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} anonymous -> {resp.status_code}: {_body(resp)[:200]}"
    )
    assert _leaks(_body(resp), b.identifiers() | set(values.values())) == []


# ------------------------------------------------------------------------------------------------ 3. IDOR
@pytest.mark.ac("P2-AC2")
@pytest.mark.parametrize(
    ("route", "method"),
    _route_methods([r for r in ROUTES if r.resource_kwargs], SAFE_METHODS + UNSAFE_METHODS),
)
def test_member_cannot_reach_other_orgs_resource_ids(two_orgs, route, method):
    a, b = two_orgs
    try:
        factory = factory_for(route)
    except MissingIsolationFixture as exc:
        pytest.fail(str(exc))
    if factory is None:
        pytest.skip(f"{route.id} is exempt from the IDOR probe")
    a_values = factory(a)  # resources that belong to org A
    path = route.build(org=b.slug, **a_values)  # ... requested through B's slug by B's owner
    resp = _call(api_client_for(b.owner), method, path)
    assert resp.status_code == 404, (
        f"{method.upper()} {path} as B owner -> {resp.status_code}: {_body(resp)[:300]}"
    )
    assert _leaks(_body(resp), a.identifiers() | set(a_values.values())) == []


@pytest.mark.ac("P2-AC2")
@pytest.mark.parametrize(
    "route", [pytest.param(r, id=r.id) for r in ROUTES if not r.resource_kwargs and "get" in r.methods]
)
def test_list_endpoints_never_contain_other_orgs_data(two_orgs, route):
    a, b = two_orgs
    # create one of every resource we know how to build in org A (and in B, so lists are non-empty)
    for r in ROUTES:
        if r.resource_kwargs:
            try:
                factory = factory_for(r)
            except MissingIsolationFixture:
                continue
            if factory:
                factory(a)
                factory(b)
    resp = api_client_for(b.owner).get(route.build(org=b.slug))
    if getattr(resp, "streaming", False):
        pytest.skip("streaming endpoint")
    assert resp.status_code < 500, resp.content[:300]
    assert _leaks(_body(resp), a.identifiers()) == []


# ------------------------------------------------------------------------------------------------ 5. CSRF
@pytest.mark.parametrize(("route", "method"), _route_methods(ROUTES, UNSAFE_METHODS))
def test_unsafe_methods_require_csrf_for_session_users(two_orgs, route, method):
    b = two_orgs.b
    values = _values(route, b)
    client = api_client_for(b.owner, csrf=False)
    resp = _call(client, method, route.build(org=b.slug, **values))
    assert resp.status_code == 403, f"{method.upper()} without CSRF -> {resp.status_code}"


def test_non_member_404_does_not_poison_callers_transaction(two_orgs):
    """Regression for QA defect core-finalize-rollback-outer-txn (W1; Backend fix in W2): finalize_response must
    only roll back the transaction the tenancy mixin itself entered. Rolling back whenever
    connection.in_atomic_block (non-member 404, anonymous 401) poisons any outer transaction (pytest-django,
    ATOMIC_REQUESTS, a service calling a view); the session save then fails and the client gets 400."""
    from django.db import connection

    b = two_orgs.b
    client = api_client_for(two_orgs.a.owner, savepoint_per_request=False)
    resp = client.get(f"/api/v1/orgs/{b.slug}/jobs")
    assert resp.status_code == 404
    assert connection.needs_rollback is False


def test_csrf_client_really_enforces_csrf(two_orgs):
    """Self-test of the shared client: with enforcement on and no token, Django's CSRF check must trigger."""
    from django.http import HttpResponse
    from django.test import RequestFactory
    from django.views.decorators.csrf import csrf_protect

    @csrf_protect
    def view(request):
        return HttpResponse("ok")

    assert view(RequestFactory().post("/x", {})).status_code == 403  # RequestFactory never disables CSRF
    client = api_client_for(two_orgs.a.owner)
    assert client.csrf_token and len(client.csrf_token) >= 32
