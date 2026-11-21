"""Agents (CONTRACT "W5 additions / Agents").

An agent is a tools allowlist (registered step kinds only), per-tool scopes and approval rules. A run of an agent is
a plan of tool calls, given explicitly (`steps`) or planned by Gemini from a goal (through `apps.ai` GeminiService
and its policy gate, with ONLY the tool metadata and the goal text: never row values). The plan is checked against
the agent (allowlist, scopes, max_steps, config schemas, the caller's permissions) and executed as an ordinary
workflow run, so every tool call is an audited step run. `impact="high"` tools, and tools/impacts listed in
`approval_required_for`, always get an approval step (kind `agent_action`) in front of them.

Scopes: `tool_scopes = {"bi.saved_query": {"saved_query_id": ["<uuid>", ...]}}` restricts a tool's config values to the
listed ones (a key that is not scoped is free).
"""

from __future__ import annotations

import json
from uuid import UUID

from rest_framework.exceptions import ValidationError

from apps.core import audit
from apps.core.exceptions import Conflict, Unprocessable
from apps.core.tenancy import TenantInfo

from . import registry, runner
from .models import AgentConfig, WorkflowRun

MAX_AGENT_STEPS = 20
_RESERVED_TOOLS = ("approval",)  # approvals are inserted by the policy, never chosen by the agent


class InvalidAgentPlan(Unprocessable):
    default_code = "invalid_agent_plan"
    default_detail = "The agent plan is not allowed."


def agent_payload(a: AgentConfig) -> dict:
    return {
        "id": str(a.id), "name": a.name, "description": a.description, "owner_id": str(a.owner_id),
        "model_tier": a.model_tier, "allowed_tools": a.allowed_tools, "tool_scopes": a.tool_scopes,
        "max_steps": a.max_steps, "approval_required_for": a.approval_required_for, "is_active": a.is_active,
        "created_at": a.created_at.isoformat(), "updated_at": a.updated_at.isoformat(),
    }


def clean_agent_config(data: dict, *, partial: bool = False, instance: AgentConfig | None = None) -> dict:
    out: dict = {}
    if "allowed_tools" in data or not partial:
        tools = data.get("allowed_tools")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
            raise ValidationError({"allowed_tools": ["A non-empty list of step kinds."]})
        unknown = [t for t in tools if registry.get_step(t) is None or t in _RESERVED_TOOLS]
        if unknown:
            raise ValidationError({"allowed_tools": [f"Not an available tool: {', '.join(sorted(set(unknown)))}."]})
        out["allowed_tools"] = sorted(set(tools))
    tools = out.get("allowed_tools", instance.allowed_tools if instance else [])
    if "tool_scopes" in data:
        scopes = data.get("tool_scopes") or {}
        if not isinstance(scopes, dict):
            raise ValidationError({"tool_scopes": ["An object of tool -> {config key: [allowed values]}."]})
        for tool, rules in scopes.items():
            if tool not in tools:
                raise ValidationError({"tool_scopes": [f"'{tool}' is not in allowed_tools."]})
            if not isinstance(rules, dict) or not all(isinstance(v, list) and v for v in rules.values()):
                raise ValidationError({"tool_scopes": [f"'{tool}': each scoped key needs a non-empty list."]})
        out["tool_scopes"] = scopes
    if "approval_required_for" in data:
        rules = data.get("approval_required_for") or []
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise ValidationError({"approval_required_for": ["A list of impacts (low/high) or tool kinds."]})
        bad = [r for r in rules if r not in ("low", "high") and r not in tools]
        if bad:
            raise ValidationError({"approval_required_for": [f"Unknown: {', '.join(bad)}."]})
        out["approval_required_for"] = sorted(set(rules) | {"high"})
    elif not partial:
        out["approval_required_for"] = ["high"]
    if "max_steps" in data:
        m = data["max_steps"]
        if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= MAX_AGENT_STEPS:
            raise ValidationError({"max_steps": [f"An integer between 1 and {MAX_AGENT_STEPS}."]})
        out["max_steps"] = m
    if "model_tier" in data:
        if data["model_tier"] not in ("fast", "reasoning"):
            raise ValidationError({"model_tier": ["One of: fast, reasoning."]})
        out["model_tier"] = data["model_tier"]
    for key in ("name", "description", "is_active"):
        if key in data:
            out[key] = data[key]
    if not partial and not out.get("name"):
        raise ValidationError({"name": ["This field is required."]})
    return out


def create_agent(*, data: dict, owner_id: UUID, request=None) -> AgentConfig:
    fields = clean_agent_config(data)
    if AgentConfig.objects.filter(name=fields["name"]).exists():
        raise Conflict("An agent with this name already exists.", code="duplicate_name")
    agent = AgentConfig.objects.create(owner_id=owner_id, **fields)
    audit.record("automation.agent.create", target=agent, request=request,
                 metadata={"allowed_tools": agent.allowed_tools})
    return agent


def update_agent(agent: AgentConfig, *, data: dict, request=None) -> AgentConfig:
    fields = clean_agent_config(data, partial=True, instance=agent)
    if "name" in fields and AgentConfig.objects.filter(name=fields["name"]).exclude(id=agent.id).exists():
        raise Conflict("An agent with this name already exists.", code="duplicate_name")
    for k, v in fields.items():
        setattr(agent, k, v)
    if "allowed_tools" in fields:  # drop scopes / rules of tools that were removed
        agent.tool_scopes = {k: v for k, v in (agent.tool_scopes or {}).items() if k in agent.allowed_tools}
        agent.approval_required_for = [r for r in agent.approval_required_for
                                       if r in ("low", "high") or r in agent.allowed_tools]
    agent.save()
    audit.record("automation.agent.update", target=agent, request=request, changes={k: str(v)[:200]
                                                                                    for k, v in fields.items()})
    return agent


def delete_agent(agent: AgentConfig, *, request=None) -> None:
    if WorkflowRun.objects.filter(agent=agent).exclude(status__in=WorkflowRun.TERMINAL).exists():
        raise Conflict("Cancel the agent's active runs first.", code="agent_has_active_runs")
    audit.record("automation.agent.delete", target=agent, request=request, metadata={"name": agent.name})
    agent.delete()


# ------------------------------------------------------------------------------------------------ planning
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tool": {"type": "string"}, "config_json": {"type": "string"},
                               "reason": {"type": "string"}},
                "required": ["tool", "config_json"],
            },
        },
    },
    "required": ["steps"],
}

PLAN_SYSTEM = (
    "You plan the steps of a business automation agent. Use ONLY the listed tools, at most {max_steps} steps, in "
    "execution order. For each step give the tool kind and its configuration as a JSON object string that "
    "satisfies the tool's JSON schema. Never invent ids that are not in the goal. If the goal cannot be done with "
    "the tools, return an empty list."
)


def plan_with_gemini(agent: AgentConfig, goal: str) -> list[dict]:
    """Ask Gemini for a plan. Sends ONLY the goal and tool metadata (kind, description, impact, config schema)."""
    from apps.ai.services import gemini
    from nexora.contracts import PayloadClass, PayloadManifest

    tools = []
    for kind in agent.allowed_tools:
        spec = registry.get_step(kind)
        if spec is not None:
            tools.append({"kind": kind, "description": spec.description, "impact": spec.impact,
                          "config_schema": spec.schema, "scopes": (agent.tool_scopes or {}).get(kind) or {}})
    prompt = json.dumps({"goal": goal, "tools": tools}, default=str)
    data = gemini().generate_structured(
        feature="automation", prompt_key="automation.agent_plan",
        system=PLAN_SYSTEM.format(max_steps=agent.max_steps), prompt=prompt, schema=PLAN_SCHEMA,
        payload=PayloadManifest({PayloadClass.SCHEMA: ()}), tier=agent.model_tier)
    steps = []
    for item in (data or {}).get("steps") or []:
        try:
            config = json.loads(item.get("config_json") or "{}")
        except (TypeError, ValueError):
            config = None
        steps.append({"tool": item.get("tool"), "config": config})
    return steps


def check_plan(agent: AgentConfig, steps, info: TenantInfo) -> list[dict]:
    """Validated tool calls, or InvalidAgentPlan (422) listing every problem."""
    errors: list[dict] = []
    if not isinstance(steps, list) or not steps:
        raise InvalidAgentPlan("The plan has no steps.", extra={"errors": [
            {"field": "steps", "code": "empty_plan", "message": "The plan has no steps."}]})
    if len(steps) > agent.max_steps:
        errors.append({"field": "steps", "code": "too_many_steps",
                       "message": f"The agent may take at most {agent.max_steps} steps."})
    clean = []
    for i, st in enumerate(steps):
        field = f"steps.{i}"
        tool = st.get("tool") if isinstance(st, dict) else None
        config = st.get("config") if isinstance(st, dict) else None
        if tool not in agent.allowed_tools:
            errors.append({"field": field, "code": "tool_not_allowed", "message": f"Tool '{tool}' is not allowed."})
            continue
        spec = registry.get_step(tool)
        if spec is None:
            errors.append({"field": field, "code": "tool_unavailable", "message": f"Tool '{tool}' is unavailable."})
            continue
        if not isinstance(config, dict):
            errors.append({"field": field, "code": "invalid_config", "message": "config must be an object."})
            continue
        for msg in registry.config_errors(tool, config):
            errors.append({"field": f"{field}.config", "code": "invalid_config", "message": msg})
        for key, allowed in ((agent.tool_scopes or {}).get(tool) or {}).items():
            if key in config and config[key] not in allowed:
                errors.append({"field": f"{field}.config.{key}", "code": "out_of_scope",
                               "message": f"'{key}' is outside the agent's scope."})
            if key not in config:
                errors.append({"field": f"{field}.config.{key}", "code": "out_of_scope",
                               "message": f"'{key}' is scoped and must be given."})
        if "automation.run" not in info.permissions or (spec.permission and spec.permission not in info.permissions):
            errors.append({"field": field, "code": "step_permission_denied",
                           "message": f"You do not hold the permission tool '{tool}' needs."})
        clean.append({"tool": tool, "config": config, "spec": spec})
    if errors:
        raise InvalidAgentPlan(extra={"errors": errors})
    return clean


def needs_approval(agent: AgentConfig, spec: registry.StepSpec) -> bool:
    rules = set(agent.approval_required_for or []) | {"high"}
    return spec.impact == "high" or spec.impact in rules or spec.kind in rules


def build_graph(agent: AgentConfig, calls: list[dict], goal: str) -> dict:
    steps: list[dict] = []
    prev: str | None = None
    for i, call in enumerate(calls, start=1):
        spec = call["spec"]
        if needs_approval(agent, spec):
            key = f"approve_{i}"
            steps.append({"key": key, "kind": "approval", "depends_on": [prev] if prev else [], "config": {
                "title": f"Agent '{agent.name}' wants to run {spec.kind}"[:200],
                "approver_permission": "approvals.decide", "impact": "high" if spec.impact == "high" else "medium",
                "kind": "agent_action", "details": {"tool": spec.kind, "config": call["config"], "goal": goal[:500]},
            }})
            prev = key
        key = f"tool_{i}"
        steps.append({"key": key, "kind": spec.kind, "config": call["config"], "depends_on": [prev] if prev else []})
        prev = key
    return {"steps": steps}


def start_agent_run(agent: AgentConfig, *, user_id: UUID, info: TenantInfo, goal: str = "",
                    steps: list | None = None, request=None) -> WorkflowRun:
    if not agent.is_active:
        raise Conflict("The agent is disabled.", code="agent_inactive")
    planned_by = "user"
    if steps is None:
        if not goal.strip():
            raise ValidationError({"goal": ["Give a goal or an explicit list of steps."]})
        steps = plan_with_gemini(agent, goal)
        planned_by = "gemini"
    calls = check_plan(agent, steps, info)
    graph = build_graph(agent, calls, goal)
    run = runner.create_run(graph=graph, actor_id=user_id, agent=agent, trigger_type="agent",
                            run_input={"goal": goal, "planned_by": planned_by}, request=request)
    audit.record("automation.agent.run", target=agent, request=request,
                 metadata={"run_id": str(run.id), "planned_by": planned_by,
                           "tools": [c["tool"] for c in calls]})
    return run
