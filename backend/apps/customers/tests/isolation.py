"""Isolation-matrix factories for /customers routes (see backend/testing/isolation.py)."""

from __future__ import annotations

import uuid
from decimal import Decimal


def _base(world):
    from apps.customers.models import CustomerProfile, CustomerProfileLink, CustomerSource, CustomerSourceRecord

    found = world.resources.get("customer_base")
    if found is None:
        with world.tenant():
            source = CustomerSource.objects.create(name=f"Iso {world.slug}", source_system=f"iso-{world.slug}"[:64],
                                                   dataset_id=uuid.uuid4(), field_mapping={"email": "email"})
            profile = CustomerProfile.objects.create(display_name="Isolation customer")
            a = CustomerSourceRecord.objects.create(source=source, source_system=source.source_system,
                                                    source_key="a", normalized={"email": "a@example.test"})
            b = CustomerSourceRecord.objects.create(source=source, source_system=source.source_system,
                                                    source_key="b", normalized={"email": "a@example.test"})
            CustomerProfileLink.objects.create(profile=profile, record=a)
            CustomerProfileLink.objects.create(profile=profile, record=b)
        found = world.resources["customer_base"] = (source, profile, a, b)
    return found


def source_factory(world) -> dict[str, str]:
    return {"pk": str(_base(world)[0].id)}


def profile_factory(world) -> dict[str, str]:
    return {"pk": str(_base(world)[1].id)}


def match_factory(world) -> dict[str, str]:
    from apps.customers.models import IdentityMatch

    match = world.resources.get("identity_match")
    if match is None:
        _s, _p, a, b = _base(world)
        with world.tenant():
            match = IdentityMatch.objects.create(record_a=a, record_b=b, score=Decimal("0.8"), status="pending_review")
        world.resources["identity_match"] = match
    return {"pk": str(match.id)}


def segment_factory(world) -> dict[str, str]:
    from apps.customers.models import CustomerSegment

    seg = world.resources.get("customer_segment")
    if seg is None:
        with world.tenant():
            seg = CustomerSegment.objects.create(name=f"Iso segment {world.slug}", method="rule", definition={
                "match": "all", "conditions": [{"field": "record_count", "op": "gte", "value": 1}]})
        world.resources["customer_segment"] = seg
    return {"pk": str(seg.id)}


def run_factory(world) -> dict[str, str]:
    from apps.customers.models import ResolutionRun

    run = world.resources.get("resolution_run")
    if run is None:
        with world.tenant():
            run = ResolutionRun.objects.create(status="succeeded")
        world.resources["resolution_run"] = run
    return {"pk": str(run.id)}


ISOLATION_FACTORIES = {
    "customer-source-detail": source_factory,
    "customer-detail": profile_factory,
    "customer-timeline": profile_factory,
    "customer-scores": profile_factory,
    "customer-match-detail": match_factory,
    "customer-match-decision": match_factory,
    "customer-segment-detail": segment_factory,
    "customer-segment-members": segment_factory,
    "customer-segment-refresh": segment_factory,
    "customer-resolution-run-detail": run_factory,
}
