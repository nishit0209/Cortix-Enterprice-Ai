"""Audit logging. Call inside a tenant context:

    from apps.core.audit import record
    record("dataset.create", target=dataset, metadata={"rows": 1000})
"""

from __future__ import annotations

from typing import Any

from .middleware import request_id_var
from .tenancy.context import current_actor, require_org_id

_SECRET_KEYS = ("password", "secret", "token", "api_key", "credential", "authorization", "cookie")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if any(s in k.lower() for s in _SECRET_KEYS) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def record(
    action: str,
    *,
    target: Any = None,
    target_type: str = "",
    target_id: str = "",
    changes: dict | None = None,
    metadata: dict | None = None,
    request: Any = None,
    actor: tuple[str, Any] | None = None,
):
    from .models import AuditEvent

    require_org_id()
    if target is not None:
        target_type = target_type or target._meta.label_lower
        target_id = target_id or str(target.pk)
    actor_type, actor_id = actor or current_actor.get()
    ip = ua = ""
    if request is not None:
        ip = request.META.get("REMOTE_ADDR", "") or ""
        ua = (request.META.get("HTTP_USER_AGENT", "") or "")[:300]
    return AuditEvent.objects.create(
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=str(target_id or ""),
        request_id=request_id_var.get(),
        ip=ip or None,
        user_agent=ua,
        changes=_redact(changes) if changes else None,
        metadata=_redact(metadata or {}),
    )
