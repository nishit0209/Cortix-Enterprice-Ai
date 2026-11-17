"""Core unit tests that need no database."""

import time
from uuid import UUID, uuid4

from apps.core import registry
from apps.core.tenancy.models import uuid7


def test_uuid7_version_variant_and_monotonic():
    ids = [uuid7() for _ in range(5000)]
    assert all(u.version == 7 and u.variant == "specified in RFC 4122" for u in ids)
    assert ids == sorted(ids, key=lambda u: u.int)
    assert len(set(ids)) == len(ids)
    ms = (ids[-1].int >> 80) / 1000
    assert abs(ms - time.time()) < 5


def test_asset_exists_registry():
    assert registry.asset_exists("unit_kind", uuid4()) is None
    known = uuid4()
    registry.register_asset_exists("unit_kind", lambda asset_id: asset_id == known)
    try:
        assert registry.asset_exists("unit_kind", known) is True
        assert registry.asset_exists("unit_kind", str(known)) is True
        assert registry.asset_exists("unit_kind", uuid4()) is False
        assert registry.asset_exists("unit_kind", "not-a-uuid") is False
    finally:
        registry._exists.pop("unit_kind", None)


def test_dataset_schema_provider_registry():
    saved = list(registry._dataset_schema_provider)
    registry._dataset_schema_provider.clear()
    try:
        assert registry.dataset_schema(uuid4()) is None
        registry.register_dataset_schema_provider(lambda ds: ({"t": f"s.{ds.hex[:4]}"}, {"t": ["a"]}))
        ds = UUID("01900000-0000-7000-8000-000000000001")
        assert registry.dataset_schema(ds) == ({"t": "s.0190"}, {"t": ["a"]})
    finally:
        registry._dataset_schema_provider[:] = saved


def test_classify_asset_fails_closed():
    assert registry.classify_asset("nope:123") == "confidential"
    assert registry.classify_asset("garbage") == "confidential"
