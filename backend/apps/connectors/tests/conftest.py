import pytest

from apps.datasets.tests.helpers import _cleanup_test_warehouse, captured_notifications, fake_storage  # noqa: F401


@pytest.fixture
def quiet_redis(fake_redis):
    """Job events go to an in-memory Redis (no network) — use in DB-backed tests."""
    return fake_redis
