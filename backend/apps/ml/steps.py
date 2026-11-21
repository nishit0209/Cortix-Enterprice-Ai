"""Automation step kinds of apps.ml (CONTRACT "W5 additions / Automation step registry").

    ml.train          train one or more algorithms on a project, optionally register the best experiment and
                      promote it (staging is immediate; production creates an approval request)
    ml.batch_predict  score a dataset with a model and publish the predictions as a dataset version

Handlers run inside the run's tenant context as the run's actor, on the automation worker, so the work runs
synchronously (a following step sees the finished result). Every handler re-checks the actor's permissions:
a workflow never lets its actor do more than the routes would.
"""

from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import PermissionDenied, ValidationError

TRAIN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "algorithms": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "random_state": {"type": "integer"},
        "register": {"type": "boolean", "default": True},
        "model_name": {"type": "string", "maxLength": 200},
        "promote_to": {"type": "string", "enum": ["none", "staging", "production"], "default": "none"},
    },
    "required": ["project_id"],
    "additionalProperties": False,
}

BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "model_id": {"type": "string", "format": "uuid"},
        "dataset_id": {"type": "string", "format": "uuid"},
        "version": {"type": "integer", "minimum": 1},
        "output_name": {"type": "string", "maxLength": 200},
        "output_dataset_id": {"type": "string", "format": "uuid"},
        "explain": {"type": "boolean", "default": False},
    },
    "required": ["model_id", "dataset_id"],
    "additionalProperties": False,
}


def step_actor_id(ctx) -> UUID | None:
    """The run's actor from a StepContext (attribute names tolerated), else the current actor ContextVar."""
    for name in ("actor_id", "user_id"):
        value = getattr(ctx, name, None)
        if value:
            return value if isinstance(value, UUID) else UUID(str(value))
    for name in ("actor", "user"):
        value = getattr(ctx, name, None)
        pk = getattr(value, "pk", None)
        if pk:
            return pk if isinstance(pk, UUID) else UUID(str(pk))
    from apps.core.tenancy.context import current_actor

    kind, actor = current_actor.get()
    return actor if kind == "user" else None


def require_actor_permission(ctx, codename: str) -> UUID:
    """No privilege escalation: the actor must hold `codename` in this org right now."""
    from apps.core.notifications import members_with_permission

    user_id = step_actor_id(ctx)
    if user_id is None or user_id not in set(members_with_permission(codename)):
        raise PermissionDenied(f"The workflow's actor lacks the {codename} permission.")
    return user_id


def train_step(ctx, config: dict) -> dict:
    from . import services

    user_id = require_actor_permission(ctx, "ml.manage")
    promote_to = config.get("promote_to") or "none"
    if promote_to != "none":
        require_actor_permission(ctx, "ml.promote")
    project = services.get_project(config["project_id"])
    data = {k: config[k] for k in ("algorithms", "random_state") if config.get(k) is not None}
    created, job, snap, result = services.start_experiments(project, user_id=user_id, data=data, inline=True)
    out: dict = {"project_id": str(project.id), "job_id": str(job.id), "snapshot_id": str(snap.id),
                 "experiments": (result or {}).get("experiments", []),
                 "best_experiment_id": (result or {}).get("best_experiment_id")}
    if not out["best_experiment_id"]:
        raise ValidationError({"ml.train": ["Every experiment failed; nothing to register."]})
    if config.get("register", True):
        experiment = services.get_experiment(out["best_experiment_id"])
        reg = {"model_name": config["model_name"]} if config.get("model_name") else {}
        version = services.register(experiment, user_id=user_id, data=reg)
        out.update(model_id=str(version.model_id), version=version.version)
        if promote_to != "none":
            outcome, obj = services.request_promotion(version.model, version.version, promote_to, user_id=user_id)
            out["stage"] = obj.stage if outcome == "promoted" else "pending_approval"
            if outcome == "pending":
                out["approval_id"] = str(obj.id)
    return out


def batch_predict_step(ctx, config: dict) -> dict:
    from . import services

    user_id = require_actor_permission(ctx, "ml.manage")
    model = services.get_model(config["model_id"])
    data = {k: config[k] for k in ("dataset_id", "version", "output_name", "output_dataset_id", "explain")
            if config.get(k) is not None}
    data.setdefault("explain", False)
    job, mv, result = services.start_batch_prediction(model, user_id=user_id, data=data, inline=True)
    if result is None:
        raise ValidationError({"ml.batch_predict": [f"Batch prediction failed; see job {job.id}."]})
    return {"job_id": str(job.id), **result}


def register_steps() -> None:
    try:
        from apps.automation.registry import register_step
    except ImportError:  # automation not installed: the ML routes work without it
        return
    register_step("ml.train", train_step, schema=TRAIN_SCHEMA, impact="low")
    register_step("ml.batch_predict", batch_predict_step, schema=BATCH_SCHEMA, impact="low")
