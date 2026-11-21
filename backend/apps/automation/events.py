"""Workflow event triggers (CONTRACT "W5 additions / Triggers").

Producers (inside the org's tenant context, or passing `org_id=`), importing lazily:

    from apps.automation.events import emit
    emit("pipeline.run_failed", {"pipeline_id": str(p.id), "run_id": str(run.id)})

Every ACTIVE `event` trigger of the org whose `config.event` equals `kind` and whose `config.filter` matches the
payload (exact values per key) starts a run of its workflow as the trigger's creator. `emit` never raises and is a
no-op when nothing matches: an event must never fail the work that produced it.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.db import transaction

log = logging.getLogger("nexora.automation")


def _matches(flt: dict, payload: dict) -> bool:
    return all(payload.get(k) == v or str(payload.get(k)) == str(v) for k, v in (flt or {}).items())


def emit(kind: str, payload: dict | None = None, *, org_id: UUID | str | None = None) -> int:
    """Start the matching workflows. Returns how many runs were started."""
    from apps.core.tenancy.context import current_org, tenant_context

    try:
        org = current_org.get() or (UUID(str(org_id)) if org_id else None)
        if org is None:
            return 0
        payload = dict(payload or {})
        with tenant_context(org):
            return _emit(kind, payload)
    except Exception:  # noqa: BLE001
        log.exception("automation event %s could not be dispatched", kind)
        return 0


def _emit(kind: str, payload: dict) -> int:
    from .models import WorkflowTrigger
    from .services import _as_trigger_actor, start_run

    started = 0
    triggers = (WorkflowTrigger.objects.select_related("workflow")
                .filter(type="event", is_active=True, config__event=kind, workflow__status="active"))
    for t in triggers:
        if not _matches((t.config or {}).get("filter") or {}, payload):
            continue
        try:
            with transaction.atomic(), _as_trigger_actor(t):
                start_run(t.workflow, actor_id=t.created_by, trigger=t, trigger_type="event",
                          trigger_payload={"event": kind, **payload})
                from django.utils import timezone

                t.last_fired_at = timezone.now()
                t.save(update_fields=["last_fired_at", "updated_at"])
            started += 1
        except Exception:  # noqa: BLE001 - one broken workflow never blocks the others
            log.exception("event trigger %s failed", t.id)
    return started
