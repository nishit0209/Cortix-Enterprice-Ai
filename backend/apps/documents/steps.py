"""Automation step kind of apps.documents (CONTRACT "W5 additions / Automation step registry").

    documents.process   (re)process documents synchronously: extract -> OCR -> chunk -> embed -> persist

Runs in the run's tenant context as the run's actor, after re-checking `documents.manage`.
"""

from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import PermissionDenied, ValidationError

MAX_DOCUMENTS_PER_STEP = 20

PROCESS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "document_id": {"type": "string", "format": "uuid"},
        "document_ids": {"type": "array", "items": {"type": "string", "format": "uuid"},
                         "maxItems": MAX_DOCUMENTS_PER_STEP},
    },
    "additionalProperties": False,
}


def _actor_id(ctx) -> UUID | None:
    for name in ("actor_id", "user_id"):
        value = getattr(ctx, name, None)
        if value:
            return value if isinstance(value, UUID) else UUID(str(value))
    for name in ("actor", "user"):
        pk = getattr(getattr(ctx, name, None), "pk", None)
        if pk:
            return pk if isinstance(pk, UUID) else UUID(str(pk))
    from apps.core.tenancy.context import current_actor

    kind, actor = current_actor.get()
    return actor if kind == "user" else None


def require_actor_permission(ctx, codename: str) -> UUID:
    """No privilege escalation: the actor must hold `codename` in this org right now."""
    from apps.core.notifications import members_with_permission

    user_id = _actor_id(ctx)
    if user_id is None or user_id not in set(members_with_permission(codename)):
        raise PermissionDenied(f"The workflow's actor lacks the {codename} permission.")
    return user_id


def process_step(ctx, config: dict) -> dict:
    from . import services

    user_id = require_actor_permission(ctx, "documents.manage")
    ids = list(config.get("document_ids") or [])
    if config.get("document_id"):
        ids.insert(0, config["document_id"])
    ids = list(dict.fromkeys(str(i) for i in ids))
    if not ids:
        raise ValidationError({"document_id": ["Give document_id or document_ids."]})
    if len(ids) > MAX_DOCUMENTS_PER_STEP:
        raise ValidationError({"document_ids": [f"At most {MAX_DOCUMENTS_PER_STEP} documents per step."]})
    docs = [services.get_document(i) for i in ids]  # 404 for another org's id, before any work
    out = []
    for doc in docs:
        job, result = services.process_now(doc, user_id=user_id)
        out.append({"document_id": str(doc.id), "job_id": str(job.id),
                    "status": "ready" if result else "failed", **({k: result[k] for k in (
                        "pages", "chunks", "entities", "tables", "doc_type")} if result else {})})
    return {"documents": out, "processed": sum(1 for o in out if o["status"] == "ready"),
            "failed": sum(1 for o in out if o["status"] == "failed")}


def register_steps() -> None:
    try:
        from apps.automation.registry import register_step
    except ImportError:
        return
    register_step("documents.process", process_step, schema=PROCESS_SCHEMA, impact="low")
