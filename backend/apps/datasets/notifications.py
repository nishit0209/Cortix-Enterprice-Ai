"""In-app notifications for the data apps (datasets, connectors, pipelines), re-exported by `services`.

Producers call `notify_users(user_ids, kind, title, body, link)` inside the org's tenant context. It delegates to
`apps.core.notifications.notify` (Backend, CONTRACT "W3 additions / Notifications"), which is imported lazily:
when it is not installed the call is a logged no-op. A notification is never allowed to break the work that
produced it: failures are logged and the write runs in a savepoint so the caller's transaction stays usable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from uuid import UUID

from django.db import transaction

log = logging.getLogger(__name__)

# kinds (stable strings; the UI may group/filter on them)
DATASET_READY = "dataset.ready"
DATASET_FAILED = "dataset.failed"
IMPORT_FINISHED = "import.finished"
IMPORT_FAILED = "import.failed"
PIPELINE_RUN_SUCCEEDED = "pipeline_run.succeeded"
PIPELINE_RUN_FAILED = "pipeline_run.failed"

TITLE_MAX = 200
BODY_MAX = 1000


def _notify_fn():
    try:
        from apps.core.notifications import notify
    except ImportError:
        return None
    return notify


def org_link(path: str) -> str:
    """App URL of a page in the active org: `/o/<slug>/<path>` (falls back to the bare path)."""
    path = path.lstrip("/")
    try:
        from apps.orgs.services import get_current_org

        return f"/o/{get_current_org().slug}/{path}"
    except Exception:  # noqa: BLE001 - a link is a convenience; never fail the notification for it
        return f"/{path}"


def recipients(*user_ids) -> list[UUID]:
    """Distinct, non-empty user ids as UUIDs (invalid ids are dropped)."""
    out: list[UUID] = []
    for uid in user_ids:
        if not uid:
            continue
        try:
            value = uid if isinstance(uid, UUID) else UUID(str(uid))
        except ValueError:
            continue
        if value not in out:
            out.append(value)
    return out


def notify_users(user_ids: Iterable, kind: str, title: str, body: str = "", link: str = "") -> bool:
    """Send one notification to each user. Returns True when it was handed to the notifications service."""
    ids = recipients(*user_ids)
    if not ids:
        return False
    notify = _notify_fn()
    if notify is None:
        log.info("notifications service not installed; skipped %s for %d user(s)", kind, len(ids))
        return False
    try:
        with transaction.atomic():
            notify(ids, kind, title[:TITLE_MAX], (body or "")[:BODY_MAX], link or "")
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.exception("notification %s could not be created", kind)
        return False
    return True
