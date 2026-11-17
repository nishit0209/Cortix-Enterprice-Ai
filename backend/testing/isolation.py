"""Resource factories for the tenant-isolation endpoint matrix (TESTING_STRATEGY §4.1).

For every tenant route with resource kwargs (pk, job_id, dataset_id, ...) the matrix needs a REAL resource that
belongs to org A, so it can prove that a member of org B cannot reach it through B's slug (IDOR check).

Resolution order for a route:
  1. `ISOLATION_FACTORIES[route_name]` in `apps.<app>.tests.isolation` (owned by that app's agent), then the
     core factories below;
  2. `KWARG_FACTORIES[kwarg]` (same modules) — e.g. every `dataset_id` route can share one factory.
A factory is `fn(world: OrgWorld) -> dict[str, str]` returning kwarg values for resources it created in `world`.
Routes that need no fixture can be listed in `ISOLATION_EXEMPT = {route_name: "reason"}`.

A route with resource kwargs and no factory raises `MissingIsolationFixture` (the suite fails) so a new endpoint
cannot silently skip isolation coverage.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from functools import cache

from .factories import OrgWorld, make_job

Factory = Callable[[OrgWorld], dict[str, str]]


class MissingIsolationFixture(LookupError):
    pass


def _job_factory(kwarg: str) -> Factory:
    def build(world: OrgWorld) -> dict[str, str]:
        job = world.resources.get("job") or make_job(world.org, user=world.owner, type="isolation.probe")
        world.resources["job"] = job
        return {kwarg: str(job.id)}

    return build


CORE_ROUTE_FACTORIES: dict[str, Factory] = {
    "job-detail": _job_factory("pk"),
    "job-events": _job_factory("job_id"),
}
CORE_KWARG_FACTORIES: dict[str, Factory] = {"job_id": _job_factory("job_id")}


def _app_labels() -> list[str]:
    from django.conf import settings

    return [a.split(".")[-1] for a in getattr(settings, "NEXORA_APPS", []) if a.startswith("apps.")]


@cache
def registry() -> tuple[dict[str, Factory], dict[str, Factory], dict[str, str]]:
    routes: dict[str, Factory] = dict(CORE_ROUTE_FACTORIES)
    kwargs: dict[str, Factory] = dict(CORE_KWARG_FACTORIES)
    exempt: dict[str, str] = {}
    for label in _app_labels():
        try:
            mod = importlib.import_module(f"apps.{label}.tests.isolation")
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.startswith(f"apps.{label}.tests"):
                continue
            raise
        routes.update(getattr(mod, "ISOLATION_FACTORIES", {}))
        kwargs.update(getattr(mod, "KWARG_FACTORIES", {}))
        exempt.update(getattr(mod, "ISOLATION_EXEMPT", {}))
    return routes, kwargs, exempt


def factory_for(route) -> Factory | None:
    """A combined factory for `route`, None when the route is exempt. Raises MissingIsolationFixture."""
    routes, kwargs, exempt = registry()
    if route.name and route.name in exempt:
        return None
    if route.name and route.name in routes:
        return routes[route.name]
    needed = route.resource_kwargs
    missing = [k for k in needed if k not in kwargs]
    if missing:
        raise MissingIsolationFixture(
            f"route {route.id!r} ({route.pattern}) has resource kwargs {missing} without an isolation factory. "
            "Add ISOLATION_FACTORIES / KWARG_FACTORIES to apps/<app>/tests/isolation.py (see testing/isolation.py)."
        )

    def combined(world: OrgWorld) -> dict[str, str]:
        values: dict[str, str] = {}
        for k in needed:
            values.update(kwargs[k](world))
        return values

    return combined
