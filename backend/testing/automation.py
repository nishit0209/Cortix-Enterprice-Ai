"""Automation helpers (CONTRACT "W5 (FINAL wave) additions"; API_SPECIFICATION Phase 10).

Everything that depends on the exact request/response SHAPES of `apps.automation` lives here, so the security and
end-to-end suites read as behaviour and a shape change is fixed in one place:

    create_workflow(client, slug, [step("notify", config={...})])   -> workflow JSON (published when routed)
    start_run(client, slug, wf_id, capture)                         -> run JSON (eager runner in tests)
    create_webhook_trigger(client, slug, wf_id)                     -> (trigger_id, secret)
    Webhook(trigger_id, secret).calibrate() / .send(...)            -> signed POST /api/v1/hooks/{trigger_id}
    decide(client, slug, approval_id, "approve", password=...)      -> POST /approvals/{id}/decision
    request_approval(world, ...)                                    -> apps.automation.services.request_approval
    registered_kinds() / qa_step(kind, impact)                      -> apps.automation.registry

The binding Python names (`registry.register_step`, `services.request_approval`) are used as specified. Request
bodies send the documented field names first and fall back to close spellings, never to anything looser.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .client import route_exists, tenant_route_exists
from .ml import pick

HAS_WORKFLOWS = tenant_route_exists("workflows")
HAS_APPROVALS = tenant_route_exists("approvals")
HAS_AGENTS = tenant_route_exists("agents")
SKIP_REASON = "apps.automation routes not built yet (Backend W5)"
OK = (200, 201, 202)
REFUSED = (400, 401, 403, 404, 409, 410, 422)


def hooks_routed() -> bool:
    return route_exists(f"/api/v1/hooks/{uuid.uuid4()}")


def base(slug: str) -> str:
    return f"/api/v1/orgs/{slug}"


def body_of(resp) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 - non-JSON body
        return {}


# ------------------------------------------------------------------------------------------------ registry / models
def registry_module():
    import importlib

    return importlib.import_module("apps.automation.registry")


def _registry_dicts() -> list[dict]:
    mod = registry_module()
    return [v for v in vars(mod).values() if isinstance(v, dict) and any(k in v for k in ("condition", "notify"))]


def registered_kinds() -> set[str]:
    """Every registered step kind (built-ins + app steps)."""
    try:
        mod = registry_module()
    except ImportError:
        return set()
    for name in ("registered_kinds", "step_kinds", "kinds", "all_steps", "list_steps"):
        fn = getattr(mod, name, None)
        if callable(fn):
            try:
                out = fn()
            except TypeError:
                continue
            return {getattr(x, "kind", x) for x in (out.keys() if isinstance(out, dict) else out)}
    kinds: set[str] = set()
    for d in _registry_dicts():
        kinds |= set(d)
    return kinds


@contextmanager
def qa_step(kind: str, *, impact: str = "low") -> Iterator[list[dict]]:
    """Register a probe step kind for the duration of a test. Yields the list of calls the handler received
    (each: {"config", "org_id", "actor"}) and removes the registration afterwards."""
    from apps.core.tenancy.context import get_current_org_id

    calls: list[dict] = []

    def handler(ctx, config: dict) -> dict:
        actor = None
        for attr in ("actor", "user", "actor_user", "actor_id", "user_id"):
            if getattr(ctx, attr, None) is not None:
                actor = getattr(ctx, attr)
                break
        calls.append({"config": dict(config or {}), "org_id": str(get_current_org_id() or ""),
                      "actor": str(getattr(actor, "pk", actor)) if actor is not None else None})
        return {"ok": True}

    registry_module().register_step(kind, handler, schema={"type": "object"}, impact=impact)
    try:
        yield calls
    finally:
        for d in _registry_dicts():
            d.pop(kind, None)
        unregister = getattr(registry_module(), "unregister_step", None)
        if callable(unregister):
            try:
                unregister(kind)
            except Exception:  # noqa: BLE001 - already removed above
                pass


def model(name: str):
    from django.apps import apps as django_apps

    for m in django_apps.get_app_config("automation").get_models():
        if m.__name__ == name:
            return m
    raise LookupError(f"apps.automation has no model {name}")


def run_model():
    from django.apps import apps as django_apps

    models = list(django_apps.get_app_config("automation").get_models())
    for m in models:
        if m.__name__ in ("WorkflowRun", "Run"):
            return m
    return next(m for m in models if m.__name__.endswith("Run") and "Step" not in m.__name__)


def trigger_model():
    from django.apps import apps as django_apps

    return next(m for m in django_apps.get_app_config("automation").get_models() if m.__name__.endswith("Trigger"))


def run_count(world) -> int:
    with world.tenant():
        return run_model().objects.count()


def approval_model():
    return model("ApprovalRequest")


def approval_status(world, approval_id) -> str:
    with world.tenant():
        return str(approval_model().objects.get(id=approval_id).status)


# ------------------------------------------------------------------------------------------------ workflows
def step(kind: str, key: str = "s1", config: dict | None = None) -> dict:
    return {"key": key, "kind": kind, "config": config or {}}


def _step_styles(steps: list[dict]) -> list[list[dict]]:
    return [
        steps,
        [{"id": s["key"], "type": s["kind"], "config": s["config"]} for s in steps],
        [{"id": s["key"], "key": s["key"], "kind": s["kind"], "type": s["kind"], "config": s["config"]} for s in steps],
    ]


def _edges(steps: list[dict]) -> list[dict]:
    keys = [s.get("key") or s.get("id") for s in steps]
    return [{"source": a, "target": b, "from": a, "to": b} for a, b in zip(keys, keys[1:], strict=False)]


def _bodies(name: str, steps: list[dict]) -> Iterator[dict]:
    for styled in _step_styles(steps):
        yield {"name": name, "graph": {"steps": styled, "edges": _edges(styled)}}
        yield {"name": name, "graph": {"nodes": styled, "edges": _edges(styled)}}
        yield {"name": name, "definition": {"steps": styled}}
        yield {"name": name, "steps": styled}


def create_workflow(client, slug: str, steps: list[dict], *, name: str | None = None, publish: bool = True,
                    expect_ok: bool = True):
    """POST /workflows (then /publish). Returns the workflow JSON, or the last refusal response when
    `expect_ok=False` and every body shape was refused."""
    name = name or f"QA workflow {uuid.uuid4().hex[:8]}"
    last = None
    for body in _bodies(name, steps):
        resp = client.post(f"{base(slug)}/workflows", body, format="json")
        if resp.status_code in (200, 201):
            wf = resp.json()
            if publish:
                publish_workflow(client, slug, str(wf["id"]))
            return wf
        last = resp
        if resp.status_code != 400:
            break  # 403/404/...: a refusal, not a shape problem
    if expect_ok:
        raise AssertionError(f"POST workflows refused every body shape: {last.status_code} {last.content[:600]}")
    return last


def publish_workflow(client, slug: str, wf_id: str) -> None:
    if not tenant_route_exists(f"workflows/{uuid.uuid4()}/publish"):
        return
    resp = client.post(f"{base(slug)}/workflows/{wf_id}/publish", {}, format="json")
    assert resp.status_code in (200, 201, 202, 204), (resp.status_code, resp.content[:400])


def run_id_of(body: dict) -> str:
    return str(pick(body, "run.id", "run_id", "id"))


def start_run(client, slug: str, wf_id: str, capture, *, inputs: dict | None = None):
    """POST /workflows/{id}/runs (on_commit callbacks executed: the eager runner finishes the run)."""
    with capture(execute=True):
        return client.post(f"{base(slug)}/workflows/{wf_id}/runs", {"inputs": inputs or {}, "context": inputs or {}},
                           format="json")


def run_detail(client, slug: str, run_id: str) -> dict:
    resp = client.get(f"{base(slug)}/workflow-runs/{run_id}")
    assert resp.status_code == 200, (resp.status_code, resp.content[:400])
    return resp.json()


def run_steps(client, slug: str, run_id: str) -> list[dict]:
    resp = client.get(f"{base(slug)}/workflow-runs/{run_id}/steps")
    if resp.status_code != 200:
        return list(pick(run_detail(client, slug, run_id), "steps", "step_runs", default=[]) or [])
    body = resp.json()
    return list(body if isinstance(body, list) else pick(body, "items", "steps", "results", default=[]) or [])


def run_status(client, slug: str, run_id: str) -> str:
    return str(pick(run_detail(client, slug, run_id), "status", "run.status"))


# ------------------------------------------------------------------------------------------------ approvals
def request_approval(world, *, impact: str, permission: str, requested_by, title: str = "QA approval",
                     payload: dict | None = None, kind: str = "workflow_step"):
    """apps.automation.services.request_approval (binding signature), inside the world's tenant."""
    from django.db import transaction

    from apps.automation.services import request_approval as svc

    kwargs = {"kind": kind, "title": title, "payload": payload or {"qa": True}, "approver_permission": permission,
              "impact": impact}
    with world.tenant():
        for requester in (requested_by, getattr(requested_by, "pk", requested_by)):
            try:
                with transaction.atomic():
                    return svc(**kwargs, requested_by=requester)
            except (TypeError, ValueError):
                continue
    raise AssertionError("request_approval rejected both a User and a user id as requested_by")


def decide(client, slug: str, approval_id, decision: str = "approve", *, password: str | None = None,
           comment: str = "qa"):
    """POST /approvals/{id}/decision. `decision` approve|reject (the past tense is tried when refused as 400)."""
    body: dict[str, Any] = {"decision": decision, "comment": comment}
    if password is not None:
        body["password"] = password
    url = f"{base(slug)}/approvals/{approval_id}/decision"
    resp = client.post(url, body, format="json")
    if resp.status_code == 400 and "decision" in resp.content.decode("utf-8", "replace") and decision in (
            "approve", "reject"):
        alt = client.post(url, {**body, "decision": decision + ("d" if decision == "approve" else "ed")},
                          format="json")
        if alt.status_code != 400:
            return alt
    return resp


def error_code(resp) -> str:
    body = body_of(resp)
    return str(body.get("code", "")) if isinstance(body, dict) else ""


# ------------------------------------------------------------------------------------------------ webhooks
def _find_secret(body: Any) -> str | None:
    if isinstance(body, dict):
        for k, v in body.items():
            if "secret" in k.lower() and isinstance(v, str) and len(v) >= 16:
                return v
        for v in body.values():
            found = _find_secret(v)
            if found:
                return found
    return None


def create_webhook_trigger(client, slug: str, wf_id: str) -> tuple[str, str]:
    url = f"{base(slug)}/workflows/{wf_id}/triggers"
    last = None
    for body in ({"type": "webhook", "config": {}}, {"kind": "webhook", "config": {}},
                 {"type": "webhook", "kind": "webhook", "config": {}}):
        resp = client.post(url, body, format="json")
        if resp.status_code in (200, 201):
            data = resp.json()
            secret = _find_secret(data)
            assert secret, f"a webhook trigger must return its secret once on creation: {data}"
            return str(pick(data, "id", "trigger.id")), secret
        last = resp
    raise AssertionError(f"POST triggers refused: {last.status_code} {last.content[:400]}")


SIGNATURE_HEADERS = ("X-Nexora-Signature", "X-Signature", "X-Webhook-Signature", "X-Hub-Signature-256")
TIMESTAMP_HEADERS = ("X-Nexora-Timestamp", "X-Timestamp", "X-Webhook-Timestamp", "X-Signature-Timestamp")


def _meta(name: str) -> str:
    return "HTTP_" + name.upper().replace("-", "_")


def _hex(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


SCHEMES: dict[str, Callable[[str, bytes, str], str]] = {
    "body-hex": lambda s, b, t: _hex(s, b),
    "body-sha256=": lambda s, b, t: "sha256=" + _hex(s, b),
    "ts.body-hex": lambda s, b, t: _hex(s, t.encode() + b"." + b),
    "ts.body-sha256=": lambda s, b, t: "sha256=" + _hex(s, t.encode() + b"." + b),
    "stripe": lambda s, b, t: f"t={t},v1=" + _hex(s, t.encode() + b"." + b),
}


class Webhook:
    """Signed calls to POST /api/v1/hooks/{trigger_id} with NO session (a plain Django test client)."""

    def __init__(self, trigger_id: str, secret: str):
        from django.test import Client

        self.trigger_id, self.secret = trigger_id, secret
        self.client = Client(enforce_csrf_checks=True)
        self.scheme: str | None = None

    @property
    def path(self) -> str:
        return f"/api/v1/hooks/{self.trigger_id}"

    def raw(self, body: bytes, *, signature: str | None, timestamp: str | None, client=None):
        headers: dict[str, str] = {}
        if signature is not None:
            headers.update({_meta(h): signature for h in SIGNATURE_HEADERS})
        if timestamp is not None:
            headers.update({_meta(h): timestamp for h in TIMESTAMP_HEADERS})
        return (client or self.client).post(self.path, data=body, content_type="application/json", **headers)

    def sign(self, body: bytes, timestamp: str, *, secret: str | None = None) -> str:
        assert self.scheme, "calibrate() first"
        return SCHEMES[self.scheme](secret or self.secret, body, timestamp)

    def send(self, payload: dict | bytes, *, age_seconds: int = 0, secret: str | None = None, capture=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        ts = str(int(time.time()) - age_seconds)
        sig = self.sign(body, ts, secret=secret)
        if capture is None:
            return self.raw(body, signature=sig, timestamp=ts), body, sig, ts
        with capture(execute=True):
            return self.raw(body, signature=sig, timestamp=ts), body, sig, ts

    def calibrate(self, capture) -> str:
        """Find the signing scheme the server accepts with a fresh, correctly signed body (2xx)."""
        tried = []
        for name, fn in SCHEMES.items():
            body = json.dumps({"qa_calibration": uuid.uuid4().hex}).encode()
            ts = str(int(time.time()))
            with capture(execute=True):
                resp = self.raw(body, signature=fn(self.secret, body, ts), timestamp=ts)
            tried.append((name, resp.status_code))
            if resp.status_code in OK:
                self.scheme = name
                return name
        raise AssertionError(
            f"no documented HMAC-SHA256 signing scheme was accepted by {self.path}: {tried}. Signature headers "
            f"tried {SIGNATURE_HEADERS}, timestamp headers {TIMESTAMP_HEADERS} (unix seconds)."
        )
