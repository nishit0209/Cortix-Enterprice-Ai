"""Workflow graphs: validation (a DAG of registered steps), execution order and `{{ ... }}` templates.

    {"steps": [
        {"key": "rows", "kind": "bi.saved_query", "config": {"saved_query_id": "<uuid>"}},
        {"key": "big", "kind": "condition", "depends_on": ["rows"],
         "config": {"left": "{{ steps.rows.output.row_count }}", "op": "gt", "right": 100}},
        {"key": "ok", "kind": "approval", "depends_on": ["big"], "config": {"title": "Ship the report?"}},
        {"key": "tell", "kind": "notify", "depends_on": ["ok"], "config": {"title": "Report approved"}},
        {"key": "else", "kind": "notify", "depends_on": ["big"], "when": false, "config": {"title": "Small day"}}
    ]}

A step runs when all of its dependencies finished. A dependency on a `condition` step gates it: the step runs only
when the condition's result equals the step's `when` (default true), otherwise it is skipped; a skipped dependency
skips its dependents. Templates read the run context: `input.*`, `trigger.*`, `steps.<key>.output.*`.
"""

from __future__ import annotations

import re
from typing import Any

from . import registry

MAX_STEPS = 50
MAX_RETRIES = 5
MAX_BACKOFF_S = 3600
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STEP_FIELDS = {"key", "kind", "name", "config", "depends_on", "when", "retries", "retry_backoff_s", "timeout_s"}
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def _err(step: str | None, field: str, message: str) -> dict:
    return {"step": step, "field": field, "message": message}


def validate(graph: Any) -> list[dict]:
    """All problems of a graph (empty when it is a valid DAG of registered, correctly configured steps)."""
    if not isinstance(graph, dict) or not isinstance(graph.get("steps"), list):
        return [_err(None, "graph", "The graph must be an object with a 'steps' list.")]
    steps = graph["steps"]
    if not steps:
        return [_err(None, "steps", "A workflow needs at least one step.")]
    if len(steps) > MAX_STEPS:
        return [_err(None, "steps", f"A workflow can have at most {MAX_STEPS} steps.")]
    errors: list[dict] = []
    keys: dict[str, dict] = {}
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(_err(None, f"steps.{i}", "Each step must be an object."))
            continue
        key = step.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            errors.append(_err(None, f"steps.{i}.key", "Step keys are lowercase letters, digits and '_' (max 64)."))
            continue
        if key in keys:
            errors.append(_err(key, "key", f"Duplicate step key '{key}'."))
            continue
        keys[key] = step
        unknown = set(step) - _STEP_FIELDS
        if unknown:
            errors.append(_err(key, "step", f"Unknown field(s): {', '.join(sorted(unknown))}."))
        kind = step.get("kind")
        if not isinstance(kind, str) or registry.get_step(kind) is None:
            errors.append(_err(key, "kind", f"Unknown step kind '{kind}'."))
        else:
            for msg in registry.config_errors(kind, step.get("config", {})):
                errors.append(_err(key, "config", msg))
        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            errors.append(_err(key, "depends_on", "depends_on must be a list of step keys."))
        if "when" in step and not isinstance(step["when"], bool):
            errors.append(_err(key, "when", "when must be true or false."))
        if "name" in step and (not isinstance(step["name"], str) or len(step["name"]) > 120):
            errors.append(_err(key, "name", "name must be a string of at most 120 characters."))
        for field, lo, hi in (("retries", 0, MAX_RETRIES), ("retry_backoff_s", 0, MAX_BACKOFF_S),
                              ("timeout_s", 1, MAX_TIMEOUT_S)):
            if field in step and (not isinstance(step[field], int) or isinstance(step[field], bool)
                                  or not lo <= step[field] <= hi):
                errors.append(_err(key, field, f"{field} must be an integer between {lo} and {hi}."))
    for key, step in keys.items():
        deps = step.get("depends_on", [])
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if dep == key:
                errors.append(_err(key, "depends_on", "A step cannot depend on itself."))
            elif dep not in keys:
                errors.append(_err(key, "depends_on", f"Unknown dependency '{dep}'."))
    if not errors and topological_order(steps) is None:
        errors.append(_err(None, "depends_on", "The steps contain a dependency cycle."))
    return errors


def topological_order(steps: list[dict]) -> list[str] | None:
    """Step keys in a stable dependency order (graph order breaks ties), or None on a cycle."""
    order_index = {s["key"]: i for i, s in enumerate(steps)}
    deps = {s["key"]: set(s.get("depends_on", [])) for s in steps}
    done: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted((k for k, d in remaining.items() if d <= set(done)), key=order_index.__getitem__)
        if not ready:
            return None
        done.append(ready[0])
        remaining.pop(ready[0])
    return done


def step_map(graph: dict) -> dict[str, dict]:
    return {s["key"]: s for s in graph.get("steps", [])}


# ------------------------------------------------------------------------------------------------ templates
def lookup(context: Any, path: str) -> Any:
    cur = context
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def render(value: Any, context: dict) -> Any:
    """Resolve `{{ path }}` templates. A string that is exactly one template keeps the value's JSON type."""
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value
    whole = _TEMPLATE_RE.fullmatch(value.strip())
    if whole:
        return lookup(context, whole.group(1))

    def sub(match: re.Match) -> str:
        found = lookup(context, match.group(1))
        return "" if found is None else str(found)

    return _TEMPLATE_RE.sub(sub, value)
