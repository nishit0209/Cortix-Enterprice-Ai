"""In-app notifications (CONTRACT "W3 additions / Notifications").

Producers (any app, inside the org's tenant context):

    from apps.core.notifications import notify, notify_permission
    notify([owner_id], "pipeline.run_failed", "Pipeline 'Daily sales' failed", "Step 'load' ...",
           "/pipelines/<id>/runs/<run_id>")
    notify_permission("org.settings.edit", "ai.quota_exhausted", "AI quota exhausted", "...", "/settings/ai")

`notify` never raises for delivery problems (a notification must never fail the work that produced it); invalid
arguments (no tenant context) still raise. Links are app-relative paths ("/datasets/<id>"), never absolute URLs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from uuid import UUID

from django.db import DatabaseError, transaction
from django.utils import timezone

from .tenancy.context import require_org_id

log = logging.getLogger("nexora.notifications")

TITLE_MAX = 200
BODY_MAX = 4000
LINK_MAX = 500
MAX_RECIPIENTS = 500

# Known producer kinds (free-form strings are accepted; these keep the UI's icons/filters stable).
KINDS = (
    "pipeline.run_failed",
    "pipeline.run_succeeded",
    "import.finished",
    "import.failed",
    "dataset.ingest_finished",
    "dataset.ingest_failed",
    "document.processed",
    "document.failed",
    "invitation.accepted",
    "ai.quota_exhausted",
)

_members_with_permission: list[Callable[[str], Iterable[UUID]]] = []


def register_members_with_permission(fn: Callable[[str], Iterable[UUID]]) -> None:
    """apps.orgs registers how to list the active members holding a permission (inside the tenant context)."""
    _members_with_permission[:] = [fn]


def members_with_permission(codename: str) -> list[UUID]:
    if not _members_with_permission:
        return []
    return list(_members_with_permission[0](codename))


def _clean_link(link: str | None) -> str:
    link = str(link or "").strip()
    if not link:
        return ""
    if not link.startswith("/") or link.startswith("//") or any(ord(c) < 32 for c in link):
        return ""  # only app-relative paths: a notification never points off-site
    return link[:LINK_MAX]


def notify(user_ids: Iterable[UUID | str] | UUID | str, kind: str, title: str, body: str = "",
           link: str | None = "") -> int:
    """Create one notification per distinct user (inside a tenant context). Returns how many were created."""
    from .models import NOTIFICATION_KIND_MAX, Notification

    org_id = require_org_id()
    if isinstance(user_ids, UUID | str):
        user_ids = [user_ids]
    recipients: list[UUID] = []
    for uid in user_ids or ():
        if uid in (None, ""):
            continue
        try:
            value = uid if isinstance(uid, UUID) else UUID(str(uid))
        except ValueError:
            log.warning("notify(%s): ignoring invalid user id", kind)
            continue
        if value not in recipients:
            recipients.append(value)
    if not recipients:
        return 0
    rows = [
        Notification(org_id=org_id, user_id=uid, kind=str(kind)[:NOTIFICATION_KIND_MAX],
                     title=str(title or kind)[:TITLE_MAX], body=str(body or "")[:BODY_MAX], link=_clean_link(link))
        for uid in recipients[:MAX_RECIPIENTS]
    ]
    try:
        with transaction.atomic():  # a savepoint: a failure here never poisons the producer's transaction
            Notification.objects.bulk_create(rows)
    except DatabaseError:
        log.exception("notify(%s): could not store %s notification(s)", kind, len(rows))
        return 0
    return len(rows)


def notify_permission(codename: str, kind: str, title: str, body: str = "", link: str | None = "") -> int:
    """Notify every active member holding `codename` (e.g. org admins: "org.settings.edit")."""
    try:
        users = members_with_permission(codename)
    except DatabaseError:
        log.exception("notify_permission(%s): member lookup failed", codename)
        return 0
    return notify(users, kind, title, body, link)


def unread_count(user_id: UUID) -> int:
    from .models import Notification

    return Notification.objects.filter(user_id=user_id, read_at__isnull=True).count()


def mark_read(user_id: UUID, notification_id: UUID) -> bool:
    """Mark one of the user's notifications read. False when it does not exist for this user."""
    from .models import Notification

    item = Notification.objects.filter(id=notification_id, user_id=user_id).first()
    if item is None:
        return False
    if item.read_at is None:
        item.read_at = timezone.now()
        item.save(update_fields=["read_at", "updated_at"])
    return True


def mark_all_read(user_id: UUID) -> int:
    from .models import Notification

    now = timezone.now()
    return Notification.objects.filter(user_id=user_id, read_at__isnull=True).update(read_at=now, updated_at=now)
