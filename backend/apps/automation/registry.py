"""Automation step registry (CONTRACT "W5 additions / Automation step registry").

Each app registers its own step kinds in `AppConfig.ready()`, importing this module lazily:

    def ready(self):
        from apps.automation.registry import register_step
        register_step("pipelines.run", run_pipeline_step, schema={...}, impact="low", permission="pipelines.run")

A handler is `handler(ctx: StepContext, config: dict) -> dict` (its JSON output). The runner calls it inside the run's
tenant context, as the run's actor, after re-checking the actor's CURRENT permissions (membership, `automation.run`
and the step's own `permission`), so a workflow can never do more than the person it acts for.

A handler pauses its run by raising `PauseRun` (the built-in `approval` and `wait` steps do); any other exception
fails the step (and, after its retries, the run). Raise `StepError(code, message)` for a clean, user-facing failure.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

IMPACT_LEVELS = ("low", "high")
_KIND_RE = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"


class StepError(Exception):
    """A step failed in an expected way (`code` is stable, `message` is shown to the user)."""

    def __init__(self, code: str, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class StepPermissionDenied(StepError):
    def __init__(self, codename: str):
        super().__init__("step_permission_denied",
                         f"The user this run acts for no longer holds the '{codename}' permission.", retryable=False)
        self.codename = codename


class PauseRun(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised by a handler to pause the run: `kind` is "approval" (with `approval_id`) or "wait" (with `resume_at`)."""

    def __init__(self, kind: str, *, approval_id: UUID | None = None, resume_at: datetime | None = None,
                 output: dict | None = None):
        super().__init__(kind)
        self.kind = kind
        self.approval_id = approval_id
        self.resume_at = resume_at
        self.output = output or {}


@dataclass
class StepContext:
    org_id: UUID
    run_id: UUID
    step_run_id: UUID
    step_key: str
    kind: str
    actor_id: UUID
    permissions: frozenset[str]
    attempt: int
    deadline: float  # time.monotonic() value after which the step counts as timed out
    run_context: dict = field(default_factory=dict)
    step: dict = field(default_factory=dict)

    def has_perm(self, codename: str) -> bool:
        return codename in self.permissions

    def require(self, codename: str) -> None:
        """Re-check a permission inside a handler (e.g. one that depends on the config)."""
        if codename not in self.permissions:
            raise StepPermissionDenied(codename)

    def check_deadline(self) -> None:
        """Long handlers call this between units of work: raises once the step's timeout has passed."""
        import time

        if time.monotonic() > self.deadline:
            raise StepError("step_timeout", f"Step '{self.step_key}' exceeded its timeout.", retryable=False)


Handler = Callable[[StepContext, dict], dict]


@dataclass(frozen=True)
class StepSpec:
    kind: str
    handler: Handler
    schema: dict
    impact: str = "low"
    permission: str | None = None
    label: str = ""
    description: str = ""

    def public(self) -> dict:
        return {"kind": self.kind, "label": self.label or self.kind, "description": self.description,
                "impact": self.impact, "permission": self.permission, "schema": self.schema}


_steps: dict[str, StepSpec] = {}
_lock = threading.Lock()


def register_step(kind: str, handler: Handler, *, schema: dict, impact: str = "low", permission: str | None = None,
                  label: str = "", description: str = "") -> StepSpec:
    """Register (or replace) a step kind. `schema` is a JSON Schema for the step's `config`."""
    import re

    if not isinstance(kind, str) or not re.match(_KIND_RE, kind) or len(kind) > 64:
        raise ValueError(f"invalid step kind {kind!r}")
    if not callable(handler):
        raise TypeError(f"step {kind!r}: handler must be callable")
    if impact not in IMPACT_LEVELS:
        raise ValueError(f"step {kind!r}: impact must be one of {IMPACT_LEVELS}")
    if not isinstance(schema, dict):
        raise TypeError(f"step {kind!r}: schema must be a JSON Schema object")
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema)
    spec = StepSpec(kind=kind, handler=handler, schema=schema, impact=impact, permission=permission,
                    label=label, description=description)
    with _lock:
        _steps[kind] = spec
    return spec


def unregister_step(kind: str) -> None:
    with _lock:
        _steps.pop(kind, None)


def get_step(kind: str) -> StepSpec | None:
    with _lock:
        return _steps.get(kind)


def step_kinds() -> list[str]:
    with _lock:
        return sorted(_steps)


def all_steps() -> list[StepSpec]:
    with _lock:
        return [_steps[k] for k in sorted(_steps)]


def config_errors(kind: str, config: Any) -> list[str]:
    """Validation messages for `config` against the kind's schema (empty when valid). Template strings
    (`{{ ... }}`) are accepted wherever a value is required: they are resolved at run time."""
    spec = get_step(kind)
    if spec is None:
        return [f"Unknown step kind '{kind}'."]
    if not isinstance(config, dict):
        return ["config must be an object."]
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(spec.schema)
    out = []
    for err in sorted(validator.iter_errors(config), key=lambda e: list(e.path)):
        if err.validator == "type" and _is_template(err.instance):
            continue
        where = ".".join(str(p) for p in err.path)
        out.append(f"{where}: {err.message}" if where else err.message)
    return out


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value and "}}" in value
