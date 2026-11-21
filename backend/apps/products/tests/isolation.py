"""Isolation-matrix factories for /products routes (see backend/testing/isolation.py)."""

from __future__ import annotations

import uuid
from decimal import Decimal


def _records(world):
    from apps.products.models import ProductMaster, ProductSource, ProductSourceRecord

    found = world.resources.get("product_records")
    if found is None:
        with world.tenant():
            source = ProductSource.objects.create(name=f"Iso {world.slug}", source_system=f"iso-{world.slug}"[:64],
                                                  dataset_id=uuid.uuid4(), field_mapping={"name": "n"})
            master = ProductMaster.objects.create(name="Isolation product", values={"name": "Isolation product"})
            a = ProductSourceRecord.objects.create(source=source, source_system=source.source_system, source_key="a",
                                                   normalized={"name": "x"}, master=master)
            b = ProductSourceRecord.objects.create(source=source, source_system=source.source_system, source_key="b",
                                                   normalized={"name": "x"}, master=master)
        found = world.resources["product_records"] = (source, master, a, b)
    return found


def source_factory(world) -> dict[str, str]:
    return {"pk": str(_records(world)[0].id)}


def master_factory(world) -> dict[str, str]:
    return {"pk": str(_records(world)[1].id)}


def match_factory(world) -> dict[str, str]:
    from apps.products.models import ProductMatch

    match = world.resources.get("product_match")
    if match is None:
        _s, _m, a, b = _records(world)
        with world.tenant():
            match = ProductMatch.objects.create(record_a=a, record_b=b, score=Decimal("0.8"), status="pending_review")
        world.resources["product_match"] = match
    return {"pk": str(match.id)}


def correction_factory(world) -> dict[str, str]:
    from apps.products.models import ProductCorrection

    c = world.resources.get("product_correction")
    if c is None:
        master = _records(world)[1]
        with world.tenant():
            c = ProductCorrection.objects.create(master=master, field="name", new_value="Renamed")
        world.resources["product_correction"] = c
    return {"pk": str(c.id)}


def run_factory(world) -> dict[str, str]:
    from apps.products.models import ProductRun

    run = world.resources.get("product_run")
    if run is None:
        with world.tenant():
            run = ProductRun.objects.create(kind="normalize", status="succeeded")
        world.resources["product_run"] = run
    return {"pk": str(run.id)}


ISOLATION_FACTORIES = {
    "product-source-detail": source_factory,
    "product-detail": master_factory,
    "product-attributes": master_factory,
    "product-match-detail": match_factory,
    "product-match-decision": match_factory,
    "product-correction-decision": correction_factory,
    "product-run-detail": run_factory,
}
