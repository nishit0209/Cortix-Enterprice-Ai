"""Glue between Data Engineering apps and `apps.automation` (CONTRACT "W5 additions").

* `register_steps(...)`: registers step kinds with `apps.automation.registry.register_step`, imported lazily. When
  the automation app (or its registry) is not installed, nothing is registered and nothing fails.
* `emit_event(kind, payload, org_id=...)`: `apps.automation.events.emit(kind, payload)` inside the org's tenant
  context, imported lazily; a no-op without automation. Never raises: events must not break ingestion or runs.
* `step_actor(ctx)` / `require_step_permission(ctx, codename)`: every step re-checks the run actor's permission in
  the current org (no privilege escalation through workflows).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from uuid import UUID

from apps.core.exceptions import Forbidden
from apps.core.tenancy import tenant_context
from apps.core.tenancy.context import current_actor, current_org

log = logging.getLogger(__name__)

# Event kinds produced by Data Engineering (CONTRACT W5 "Triggers").
DATASET_INGEST_FINISHED = "dataset.ingest_finished"
PIPELINE_RUN_FAILED = "pipeline.run_failed"
PIPELINE_RUN_SUCCEEDED = "pipeline.run_succeeded"


# ------------------------------------------------------------------------------------------------ registration
def _registry():
    try:
        from apps.automation import registry
    except ImportError:
        return None
    return registry if callable(getattr(registry, "register_step", None)) else None


def register_steps(specs: Iterable[tuple[str, Callable, dict, str]]) -> list[str]:
    """[(kind, handler, schema, impact)] -> registered kinds. Called from AppConfig.ready()."""
    registry = _registry()
    if registry is None:
        log.debug("apps.automation.registry not available; steps not registered")
        return []
    done = []
    for kind, handler, schema, impact in specs:
        try:
            registry.register_step(kind, handler, schema=schema, impact=impact)
            done.append(kind)
        except Exception:  # noqa: BLE001 - a duplicate registration (autoreload) must not break startup
            log.warning("automation step %s not registered", kind, exc_info=True)
    return done


# ------------------------------------------------------------------------------------------------ events
def emit_event(kind: str, payload: Mapping[str, Any], *, org_id) -> bool:
    """Emit an automation event for `org_id`. Returns True when the emitter ran. Never raises."""
    try:
        from apps.automation import events
    except ImportError:
        return False
    emit = getattr(events, "emit", None)
    if not callable(emit):
        return False
    body = {k: (str(v) if isinstance(v, UUID) else v) for k, v in payload.items()}
    body.setdefault("org_id", str(org_id))
    try:
        org = org_id if isinstance(org_id, UUID) else UUID(str(org_id))
        if current_org.get() == org:
            emit(kind, body)
        else:
            with tenant_context(org):
                emit(kind, body)
        return True
    except Exception:  # noqa: BLE001 - automation is downstream; the producer must keep working
        log.exception("automation event %s failed for org %s", kind, org_id)
        return False


def on_dataset_ingest_finished(sender, *, org_id, dataset_id, job_id, ok, code=None, **kwargs) -> None:
    """Receiver of apps.datasets.signals.dataset_ingest_finished -> `dataset.ingest_finished`."""
    payload: dict[str, Any] = {"dataset_id": dataset_id, "job_id": job_id, "ok": bool(ok), "code": code or None,
                               "status": "succeeded" if ok else "failed"}
    try:
        from apps.core.models import Job

        from .models import Dataset

        with tenant_context(org_id if isinstance(org_id, UUID) else UUID(str(org_id))):
            ds = Dataset.objects.filter(id=dataset_id).values("name", "classification").first()
            job = Job.objects.filter(id=job_id).values("result").first()
        if ds:
            payload.update(dataset_name=ds["name"], classification=ds["classification"])
        result = (job or {}).get("result") or {}
        for key in ("version_id", "version", "row_count", "quality_score", "current"):
            if key in result:
                payload[key] = result[key]
    except Exception:  # noqa: BLE001 - the event still goes out with the ids
        log.warning("ingest event details unavailable for job %s", job_id, exc_info=True)
    emit_event(DATASET_INGEST_FINISHED, payload, org_id=org_id)


# ------------------------------------------------------------------------------------------------ step helpers
def _uuid(value) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(getattr(value, "pk", value)))
    except (TypeError, ValueError, AttributeError):
        return None


def step_actor(ctx) -> UUID | None:
    """The run actor's user id from the automation StepContext (several spellings accepted), else the current
    audit actor when it is a user."""
    for name in ("actor_id", "user_id", "actor", "user"):
        found = _uuid(getattr(ctx, name, None))
        if found is not None:
            return found
    run = getattr(ctx, "run", None)
    for name in ("actor_id", "created_by", "requested_by", "triggered_by"):
        found = _uuid(getattr(run, name, None)) if run is not None else None
        if found is not None:
            return found
    actor_type, actor_id = current_actor.get()
    return _uuid(actor_id) if actor_type == "user" else None


def require_step_permission(ctx, codename: str) -> UUID:
    """The actor's user id when they hold `codename` in the current org; else 403 `forbidden`."""
    from apps.core.notifications import members_with_permission

    actor = step_actor(ctx)
    if actor is None:
        raise Forbidden("This step needs a user actor.", code="automation_actor_required")
    if actor not in set(members_with_permission(codename)):
        raise Forbidden(f"The workflow actor lacks the permission {codename}.", extra={"permission": codename})
    return actor


def config_value(config: Mapping[str, Any] | None, key: str, default=None):
    return (config or {}).get(key, default)
