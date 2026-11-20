"""Isolation-matrix factories for governance routes (see backend/testing/isolation.py).

Dataset-scoped routes use the `dataset_id` kwarg factory of the datasets app when one is registered (a REAL
dataset), otherwise a synthetic dataset UUID that only org A's governance rows reference."""

from __future__ import annotations

import uuid


def dataset_id_for(world) -> str:
    from testing.isolation import registry

    _routes, kwargs, _exempt = registry()
    factory = kwargs.get("dataset_id")
    if factory is not None:
        return factory(world)["dataset_id"]
    if "governance_dataset_id" not in world.resources:
        world.resources["governance_dataset_id"] = uuid.uuid4()
    return str(world.resources["governance_dataset_id"])


def _acl(world) -> dict[str, str]:
    from apps.governance.models import DatasetAcl
    from apps.orgs.models import Role

    ds = dataset_id_for(world)
    acl = world.resources.get("dataset_acl")
    if acl is None:
        with world.tenant():
            acl = DatasetAcl.objects.create(dataset_id=ds, principal_type="role",
                                            principal_id=Role.objects.get(key="analyst").id, privilege="query")
        world.resources["dataset_acl"] = acl
    return {"dataset_id": str(acl.dataset_id), "pk": str(acl.id)}


def _column_policy(world) -> dict[str, str]:
    from apps.governance.models import ColumnPolicy

    ds = dataset_id_for(world)
    pol = world.resources.get("column_policy")
    if pol is None:
        with world.tenant():
            pol = ColumnPolicy.objects.create(dataset_id=ds, column_name="customer_email", principal_type="everyone",
                                              action="mask_hash")
        world.resources["column_policy"] = pol
    return {"dataset_id": str(pol.dataset_id), "pk": str(pol.id)}


def _row_policy(world) -> dict[str, str]:
    from apps.governance.models import RowPolicy

    ds = dataset_id_for(world)
    pol = world.resources.get("row_policy")
    if pol is None:
        with world.tenant():
            pol = RowPolicy.objects.create(dataset_id=ds, name="isolation-west", principal_type="everyone",
                                           predicate={"all": [{"col": "region", "op": "=", "value": "West"}]})
        world.resources["row_policy"] = pol
    return {"dataset_id": str(pol.dataset_id), "pk": str(pol.id)}


def _dataset_only(world) -> dict[str, str]:
    return {"dataset_id": dataset_id_for(world)}


def _user_attributes(world) -> dict[str, str]:
    return {"user_id": str(world.analyst.pk)}


ISOLATION_FACTORIES = {
    "dataset-acl-list": _dataset_only,
    "dataset-acl-detail": _acl,
    "column-policy-list": _dataset_only,
    "column-policy-detail": _column_policy,
    "row-policy-list": _dataset_only,
    "row-policy-detail": _row_policy,
    "policy-preview": _dataset_only,
    "user-attributes": _user_attributes,
}
