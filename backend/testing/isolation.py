"""Resource factories for the tenant-isolation endpoint matrix (TESTING_STRATEGY §4.1).

For every tenant route with resource kwargs (pk, job_id, dataset_id, ...) the matrix needs a REAL resource that
belongs to org A, so it can prove that a member of org B cannot reach it through B's slug (IDOR check).

Each app's agent registers factories in `backend/apps/<app>/tests/isolation.py`:

    from testing.isolation import once

    def _conversation(world):                       # world: testing.factories.OrgWorld
        def build():
            with world.tenant():                    # tenant context + DB setting cleanup
                return Conversation.objects.create(title="iso", dataset_id=..., created_by=world.owner.pk)
        conv = once(world, "bi.conversation", build)  # one resource per org, reused by every route
        return {"pk": str(conv.id)}

    ISOLATION_FACTORIES = {"bi-conversation-detail": _conversation}   # by route name (most specific)
    KWARG_FACTORIES = {"conversation_pk": lambda w: {"conversation_pk": ...}}   # by URL kwarg (shared)
    ISOLATION_EXEMPT = {"bi-something": "reason why no resource can exist"}

Resolution order for a route: ISOLATION_FACTORIES[route name] -> KWARG_FACTORIES for every resource kwarg.
Core routes (jobs) are registered below. A route with resource kwargs and no factory raises
`MissingIsolationFixture` with the exact file to edit and a ready-to-paste snippet, so a new endpoint cannot
silently skip isolation coverage. A factory that returns the wrong kwargs raises `IsolationFactoryError`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from functools import cache
from typing import Any

from .factories import OrgWorld, make_job

Factory = Callable[[OrgWorld], dict[str, str]]


class MissingIsolationFixture(LookupError):
    pass


class IsolationFactoryError(AssertionError):
    pass


class IsolationRegistryConflict(RuntimeError):
    pass


def once(world: OrgWorld, key: str, build: Callable[[], Any]) -> Any:
    """Create a resource at most once per org world (factories are called by many routes)."""
    if key not in world.resources:
        world.resources[key] = build()
    return world.resources[key]


def _job_factory(kwarg: str) -> Factory:
    def build(world: OrgWorld) -> dict[str, str]:
        job = once(world, "job", lambda: make_job(world.org, user=world.owner, type="isolation.probe"))
        return {kwarg: str(job.id)}

    return build


CORE_ROUTE_FACTORIES: dict[str, Factory] = {
    "job-detail": _job_factory("pk"),
    "job-events": _job_factory("job_id"),
}
CORE_KWARG_FACTORIES: dict[str, Factory] = {"job_id": _job_factory("job_id")}
CORE_MODULE = "testing.isolation"


def _app_labels() -> list[str]:
    from django.conf import settings

    return [a.split(".")[-1] for a in getattr(settings, "NEXORA_APPS", []) if a.startswith("apps.")]


def _merge(target: dict, origins: dict, items: dict, module: str, kind: str) -> None:
    for name, value in items.items():
        if name in target and origins.get(name) != module:
            raise IsolationRegistryConflict(
                f"{kind} {name!r} is registered by both {origins[name]} and {module}; keep exactly one."
            )
        target[name] = value
        origins[name] = module


@cache
def registry() -> tuple[dict[str, Factory], dict[str, Factory], dict[str, str]]:
    routes, kwargs, exempt, _origins = _registry_with_origins()
    return routes, kwargs, exempt


@cache
def _registry_with_origins():
    routes: dict[str, Factory] = dict(CORE_ROUTE_FACTORIES)
    kwargs: dict[str, Factory] = dict(CORE_KWARG_FACTORIES)
    exempt: dict[str, str] = {}
    origins: dict[str, dict[str, str]] = {
        "route": dict.fromkeys(routes, CORE_MODULE),
        "kwarg": dict.fromkeys(kwargs, CORE_MODULE),
        "exempt": {},
    }
    for label in _app_labels():
        module_name = f"apps.{label}.tests.isolation"
        try:
            mod = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and (module_name.startswith(exc.name + ".") or exc.name == module_name):
                continue
            raise
        _merge(routes, origins["route"], getattr(mod, "ISOLATION_FACTORIES", {}), module_name, "ISOLATION_FACTORIES")
        _merge(kwargs, origins["kwarg"], getattr(mod, "KWARG_FACTORIES", {}), module_name, "KWARG_FACTORIES")
        _merge(exempt, origins["exempt"], getattr(mod, "ISOLATION_EXEMPT", {}), module_name, "ISOLATION_EXEMPT")
    for name, reason in exempt.items():
        if not str(reason or "").strip():
            raise IsolationRegistryConflict(f"ISOLATION_EXEMPT[{name!r}] in {origins['exempt'][name]} needs a reason")
    return routes, kwargs, exempt, origins


def registration_origin(kind: str, name: str) -> str | None:
    """Module that registered a route factory ('route'), kwarg factory ('kwarg') or exemption ('exempt')."""
    return _registry_with_origins()[3][kind].get(name)


def owning_app(route) -> str | None:
    """App label of the view behind `route` (apps.<label>.views -> <label>)."""
    cls = route.view_class or route.callback
    module = getattr(cls, "__module__", "") or ""
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "apps" else None


def _snippet(route, missing: list[str]) -> str:
    app = owning_app(route) or "<app>"
    kw_lines = ", ".join(f'"{k}": str(obj.id)' for k in missing)
    name = route.name or "<route-name>"
    return (
        f"    # backend/apps/{app}/tests/isolation.py\n"
        f"    from testing.isolation import once\n\n"
        f"    def _factory(world):\n"
        f"        def build():\n"
        f"            with world.tenant():\n"
        f"                return <create the resource in world.org>\n"
        f"        obj = once(world, \"{app}.<resource>\", build)\n"
        f"        return {{{kw_lines}}}\n\n"
        f"    ISOLATION_FACTORIES = {{\"{name}\": _factory}}\n"
        f"    # or, shared by every route with that kwarg: KWARG_FACTORIES = {{\"{missing[0]}\": ...}}\n"
        f"    # or, when no resource can exist: ISOLATION_EXEMPT = {{\"{name}\": \"<reason>\"}}"
    )


def factory_for(route) -> Factory | None:
    """A combined factory for `route`, None when the route is exempt. Raises MissingIsolationFixture."""
    routes, kwargs, exempt = registry()
    if route.name and route.name in exempt:
        return None
    needed = route.resource_kwargs
    if route.name and route.name in routes:
        return _checked(route, routes[route.name], needed, registration_origin("route", route.name))
    missing = [k for k in needed if k not in kwargs]
    if missing:
        view = route.view_class or route.callback
        view_name = f"{getattr(view, '__module__', '?')}.{getattr(view, '__qualname__', getattr(view, '__name__', '?'))}"
        app = owning_app(route)
        raise MissingIsolationFixture(
            f"route {route.name or '<unnamed>'!r} ({route.pattern}; view {view_name}; methods "
            f"{', '.join(m.upper() for m in route.methods)}) has resource kwargs {missing} without an isolation "
            f"factory. Owner: apps/{app or '<app>'} -> add a factory to backend/apps/{app or '<app>'}/tests/"
            f"isolation.py (see backend/testing/isolation.py). Example:\n{_snippet(route, missing)}"
        )

    def combined(world: OrgWorld) -> dict[str, str]:
        values: dict[str, str] = {}
        for k in needed:
            values.update(kwargs[k](world))
        return values

    origins = sorted({registration_origin("kwarg", k) or "?" for k in needed})
    return _checked(route, combined, needed, ", ".join(origins))


def _checked(route, factory: Factory, needed: tuple[str, ...], origin: str | None) -> Factory:
    def build(world: OrgWorld) -> dict[str, str]:
        values = factory(world)
        if not isinstance(values, dict):
            raise IsolationFactoryError(
                f"isolation factory for {route.id!r} (from {origin}) returned {type(values).__name__}, expected "
                f"a dict of URL kwargs {list(needed)}"
            )
        absent = [k for k in needed if not values.get(k)]
        if absent:
            raise IsolationFactoryError(
                f"isolation factory for {route.id!r} (from {origin}) returned {sorted(values)} but the route "
                f"{route.pattern} needs non-empty values for {absent}"
            )
        return {k: str(v) for k, v in values.items()}

    return build


def stale_registrations(route_names: set[str]) -> list[str]:
    """Registered route names that match no live route (typos / renamed routes)."""
    routes, _kwargs, exempt = registry()
    problems = []
    for name in sorted(set(routes) - route_names):
        problems.append(f"ISOLATION_FACTORIES[{name!r}] in {registration_origin('route', name)} matches no route")
    for name in sorted(set(exempt) - route_names):
        problems.append(f"ISOLATION_EXEMPT[{name!r}] in {registration_origin('exempt', name)} matches no route")
    return problems
