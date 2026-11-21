"""Built-in step kinds (CONTRACT "W5"): `condition`, `notify`, `approval`, `wait`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .registry import PauseRun, StepContext, StepError, register_step

OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "exists", "truthy")
MAX_WAIT_S = 30 * 24 * 3600
MAX_NOTIFY_USERS = 50

CONDITION_SCHEMA = {
    "type": "object",
    "properties": {"left": {}, "op": {"enum": list(OPS)}, "right": {}},
    "required": ["left", "op"],
    "additionalProperties": False,
}
NOTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "body": {"type": "string", "maxLength": 4000},
        "link": {"type": "string", "maxLength": 500},
        "recipients": {"enum": ["actor", "users", "permission"]},
        "user_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}, "maxItems": MAX_NOTIFY_USERS},
        "permission": {"type": "string", "maxLength": 60},
    },
    "required": ["title"],
    "additionalProperties": False,
}
APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "approver_permission": {"type": "string", "maxLength": 60},
        "impact": {"enum": ["low", "medium", "high"]},
        "expires_in_hours": {"type": "integer", "minimum": 1, "maximum": 720},
        "details": {"type": "object"},
        "kind": {"enum": ["workflow_step", "agent_action"]},
    },
    "required": ["title"],
    "additionalProperties": False,
}
WAIT_SCHEMA = {
    "type": "object",
    "properties": {
        "seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_S},
        "until": {"type": "string"},
    },
    "additionalProperties": False,
    "oneOf": [{"required": ["seconds"]}, {"required": ["until"]}],
}


def _num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def evaluate(left, op: str, right=None) -> bool:
    if op == "exists":
        return left is not None
    if op == "truthy":
        return bool(left)
    if op == "eq":
        return left == right or (_num(left) == _num(right) and left is not None and right is not None)
    if op == "ne":
        return not evaluate(left, "eq", right)
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(left), _num(right)
        try:
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
        except TypeError as exc:
            raise StepError("invalid_comparison", f"Cannot compare {left!r} and {right!r}.", retryable=False) from exc
    if op in ("in", "not_in"):
        if not isinstance(right, list):
            raise StepError("invalid_comparison", "'in' needs a list on the right.", retryable=False)
        found = left in right
        return found if op == "in" else not found
    if op == "contains":
        if isinstance(left, str) and isinstance(right, str):
            return right in left
        if isinstance(left, list):
            return right in left
        return False
    raise StepError("invalid_comparison", f"Unknown operator {op!r}.", retryable=False)


def condition_step(ctx: StepContext, config: dict) -> dict:
    return {"result": bool(evaluate(config.get("left"), config["op"], config.get("right")))}


def notify_step(ctx: StepContext, config: dict) -> dict:
    from apps.core.notifications import notify, notify_permission
    from apps.orgs.permissions_catalog import PERMISSIONS

    mode = config.get("recipients") or ("users" if config.get("user_ids") else
                                        "permission" if config.get("permission") else "actor")
    title, body, link = config["title"], config.get("body", ""), config.get("link", "")
    if mode == "permission":
        codename = config.get("permission") or ""
        if codename not in PERMISSIONS:
            raise StepError("invalid_config", f"Unknown permission '{codename}'.", retryable=False)
        sent = notify_permission(codename, "automation.notify", title, body, link)
        return {"recipients": "permission", "permission": codename, "sent": sent}
    if mode == "users":
        from apps.orgs.services import member_permissions

        users = []
        for raw in config.get("user_ids") or []:
            try:
                uid = UUID(str(raw))
            except ValueError as exc:
                raise StepError("invalid_config", f"Not a user id: {raw!r}.", retryable=False) from exc
            if member_permissions(uid) is not None:  # only active members of this org
                users.append(uid)
        sent = notify(users, "automation.notify", title, body, link) if users else 0
        return {"recipients": "users", "sent": sent, "skipped": len(config.get("user_ids") or []) - len(users)}
    sent = notify([ctx.actor_id], "automation.notify", title, body, link)
    return {"recipients": "actor", "sent": sent}


def approval_step(ctx: StepContext, config: dict) -> dict:
    from apps.orgs.permissions_catalog import PERMISSIONS

    from .services import request_approval

    perm = config.get("approver_permission") or "approvals.decide"
    if perm not in PERMISSIONS:
        raise StepError("invalid_config", f"Unknown approver permission '{perm}'.", retryable=False)
    hours = int(config.get("expires_in_hours") or 168)
    approval = request_approval(
        kind=config.get("kind") or "workflow_step", title=config["title"],
        payload={"run_id": str(ctx.run_id), "step_key": ctx.step_key, "details": config.get("details") or {}},
        approver_permission=perm, impact=config.get("impact") or "low", requested_by=ctx.actor_id,
        source_id=ctx.step_run_id, expires_in=timedelta(hours=hours))
    raise PauseRun("approval", approval_id=approval.id, output={"approval_id": str(approval.id)})


def wait_step(ctx: StepContext, config: dict) -> dict:
    now = timezone.now()
    if config.get("seconds"):
        resume_at = now + timedelta(seconds=int(config["seconds"]))
    else:
        until = config.get("until")
        resume_at = parse_datetime(until) if isinstance(until, str) else None
        if resume_at is None:
            raise StepError("invalid_config", "'until' must be an ISO-8601 date-time.", retryable=False)
        if timezone.is_naive(resume_at):
            resume_at = resume_at.replace(tzinfo=UTC)
        if resume_at - now > timedelta(seconds=MAX_WAIT_S):
            raise StepError("invalid_config", "A wait can last at most 30 days.", retryable=False)
    if resume_at <= now:
        return {"waited_s": 0}
    raise PauseRun("wait", resume_at=resume_at, output={"resume_at": _iso(resume_at)})


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def register() -> None:
    register_step("condition", condition_step, schema=CONDITION_SCHEMA, label="Condition",
                  description="Compares two values; dependent steps run only on the matching branch (`when`).")
    register_step("notify", notify_step, schema=NOTIFY_SCHEMA, label="Notify",
                  description="Sends an in-app notification to the run's user, chosen members or a permission.")
    register_step("approval", approval_step, schema=APPROVAL_SCHEMA, label="Approval",
                  description="Pauses the run until a member holding the approver permission decides.")
    register_step("wait", wait_step, schema=WAIT_SCHEMA, label="Wait",
                  description="Pauses the run for a number of seconds or until a date-time.")
