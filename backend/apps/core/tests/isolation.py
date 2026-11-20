"""Isolation-matrix factories for core tenant routes (see backend/testing/isolation.py). Jobs are registered by
testing.isolation itself; notifications are registered here."""

from __future__ import annotations


def _notification(world) -> dict[str, str]:
    from apps.core.models import Notification

    item = world.resources.get("core_notification")
    if item is None:
        with world.tenant():
            item = Notification.objects.create(user_id=world.owner.pk, kind="isolation.probe",
                                               title="Isolation probe", body="synthetic", link="/")
        world.resources["core_notification"] = item
    return {"pk": str(item.id)}


ISOLATION_FACTORIES = {
    "notification-read": _notification,
}
