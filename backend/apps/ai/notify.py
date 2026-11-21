"""Best-effort user notifications produced by apps.ai (CONTRACT W3 "Notifications": AI quota exhausted).

`apps.core.notifications` (`notify` / `notify_permission`) is built by the Backend agent in W3; it is imported
lazily and silently skipped when it does not exist yet. A notification never fails an AI request.

Recipients are the org's admins: the active members holding `org.settings.edit` ("Edit organization settings, AI
config and budgets"), i.e. the people who can raise the budget.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import nullcontext
from datetime import datetime

from django.db import transaction

log = logging.getLogger("nexora.ai")

ADMIN_PERMISSION = "org.settings.edit"
QUOTA_KIND = "ai.quota_exhausted"
QUOTA_LINK = "/settings/ai-policy"  # app-relative; the frontend prefixes /o/<org>

SCOPE_TEXT = {
    "org_day": "Your organization's daily AI request budget is used up.",
    "org_tokens": "Your organization's daily AI token budget is used up.",
    "project_day": "The platform's daily Gemini quota is used up.",
}


def org_admin_user_ids() -> list:
    """Active members of the CURRENT tenant holding ADMIN_PERMISSION (via apps.orgs.services when available)."""
    try:
        from apps.orgs import services as org_services
    except ImportError:  # pragma: no cover - apps.orgs is always installed
        return []
    helper = getattr(org_services, "members_with_permission", None)
    if not callable(helper):
        return []
    return list(helper(ADMIN_PERMISSION))


def quota_message(scope: str, resets_at: datetime | None) -> tuple[str, str]:
    title = "AI quota exhausted for today"
    when = f" It resets at {resets_at.strftime('%Y-%m-%d %H:%M UTC')}." if resets_at else ""
    body = (SCOPE_TEXT.get(scope, "The AI quota is used up.") + when
            + " Simple questions, saved queries, dashboards and extractive document answers keep working.")
    return title, body


def notify_quota_exhausted(scope: str, resets_at: datetime | None) -> bool:
    """Notify the org admins once (the caller de-duplicates per day). Returns True when a notification was sent."""
    try:
        # import_module consults sys.modules first (a package attribute lookup would bypass replacements there)
        notifications = importlib.import_module("apps.core.notifications")
    except ImportError:
        return False
    title, body = quota_message(scope, resets_at)
    # Inside a transaction: a savepoint, so a failure never poisons the caller's transaction. Outside one there
    # is nothing to protect (and no connection is opened just for that).
    guard = transaction.atomic() if transaction.get_connection().in_atomic_block else nullcontext()
    try:
        with guard:
            send_to_permission = getattr(notifications, "notify_permission", None)
            if callable(send_to_permission):
                return bool(send_to_permission(ADMIN_PERMISSION, QUOTA_KIND, title, body, QUOTA_LINK))
            user_ids = org_admin_user_ids()
            if not user_ids:
                return False
            return bool(notifications.notify(user_ids, QUOTA_KIND, title, body, QUOTA_LINK))
    except Exception:  # noqa: BLE001 - notifications are best effort
        log.warning("ai: quota-exhausted notification failed", exc_info=True)
        return False
