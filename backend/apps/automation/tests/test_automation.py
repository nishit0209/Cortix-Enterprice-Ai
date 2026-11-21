"""Automation (W5): workflows, runner, approvals (re-auth), triggers (schedule, webhook HMAC, event), agents and the
bi.saved_query step. Real PostgreSQL with RLS; synthetic data only; Gemini is never called (fake engine)."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.utils import timezone

from apps.automation import registry, services
from apps.automation.events import emit
from apps.automation.models import (
    AgentConfig,
    ApprovalRequest,
    WebhookDelivery,
    Workflow,
    WorkflowRun,
    WorkflowStepRun,
    WorkflowTrigger,
)
from apps.automation.registry import StepError
from apps.core.models import AuditEvent, Notification
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, client_for, member_of, member_with_permissions, org_url

pytestmark = pytest.mark.django_db

NOTIFY = {"key": "hello", "kind": "notify", "config": {"title": "Hello {{ input.name }}"}}


# ------------------------------------------------------------------------------------------------ helpers
@pytest.fixture
def run_now(django_capture_on_commit_callbacks):
    """Execute on-commit callbacks (the eager task runner starts runs after commit)."""

    def call(fn, *args, **kwargs):
        with django_capture_on_commit_callbacks(execute=True):
            return fn(*args, **kwargs)

    return call


@pytest.fixture
def test_steps():
    """Test-only step kinds (registered for the test, removed afterwards)."""
    state = {"calls": 0}

    def flaky(ctx, config):
        state["calls"] += 1
        if state["calls"] <= int(config.get("fail_times", 0)):
            raise StepError("flaky", "temporary failure")
        return {"attempt": ctx.attempt}

    def slow(ctx, config):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_sleep(%s)", [config.get("seconds", 2)])
        return {}

    def danger(ctx, config):
        return {"done": True, "target": config.get("target")}

    free = {"type": "object"}
    registry.register_step("test.flaky", flaky, schema=free)
    registry.register_step("test.slow", slow, schema=free)
    registry.register_step("test.pipeline", danger, schema=free, permission="pipelines.run")
    registry.register_step("test.danger", danger, schema={
        "type": "object", "properties": {"target": {"type": "string"}}, "additionalProperties": False},
        impact="high")
    yield state
    for kind in ("test.flaky", "test.slow", "test.pipeline", "test.danger"):
        registry.unregister_step(kind)


def wurl(org, *parts) -> str:
    return org_url(org, "/".join(["workflows", *map(str, parts)]))


def make_workflow(client, org, steps, *, name="Daily check", publish=True) -> dict:
    r = client.post(wurl(org), {"name": name, "graph": {"steps": steps}}, format="json")
    assert r.status_code == 201, r.content
    wf = r.json()
    if publish:
        r = client.post(wurl(org, wf["id"], "publish"), {}, format="json")
        assert r.status_code == 200, r.content
        wf = r.json()
    return wf


def start(client, org, wf_id, run_now, payload=None):
    r = run_now(client.post, wurl(org, wf_id, "runs"), {"input": payload or {}}, format="json")
    assert r.status_code == 202, r.content
    return r.json()


def get_run(client, org, run_id) -> dict:
    r = client.get(org_url(org, f"workflow-runs/{run_id}"))
    assert r.status_code == 200, r.content
    return r.json()


def steps_by_key(run: dict) -> dict:
    return {s["step_key"]: s for s in run["steps"]}


# ------------------------------------------------------------------------------------------------ graphs
def test_invalid_graphs_are_rejected_with_errors(org, owner_client):
    cases = [
        {"steps": []},
        {"steps": [{"key": "a", "kind": "nope", "config": {}}]},
        {"steps": [{"key": "a", "kind": "notify", "config": {}}]},  # title required
        {"steps": [{"key": "a", "kind": "notify", "config": {"title": "x"}, "depends_on": ["b"]},
                   {"key": "b", "kind": "notify", "config": {"title": "y"}, "depends_on": ["a"]}]},
        {"steps": [{"key": "a", "kind": "notify", "config": {"title": "x"}, "retries": 99}]},
        {"steps": [{"key": "Bad Key", "kind": "notify", "config": {"title": "x"}}]},
    ]
    for i, graph in enumerate(cases):
        r = owner_client.post(wurl(org), {"name": f"bad {i}", "graph": graph}, format="json")
        assert r.status_code == 422, (graph, r.content)
        assert r["Content-Type"].startswith("application/problem+json")
        assert r.json()["code"] == "invalid_workflow" and r.json()["errors"]
    r = owner_client.post(wurl(org, "validate"), {"graph": cases[3]}, format="json")
    assert r.status_code == 200 and r.json()["valid"] is False
    assert "cycle" in r.json()["errors"][0]["message"]


def test_versions_publish_and_activation(org, owner_client):
    wf = make_workflow(owner_client, org, [NOTIFY], publish=False)
    assert wf["status"] == "draft" and wf["current_version"] is None
    r = owner_client.patch(wurl(org, wf["id"]), {"status": "active"}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "workflow_not_active"
    pub = owner_client.post(wurl(org, wf["id"], "publish"), {}, format="json").json()
    assert pub["status"] == "active" and pub["current_version"] == 1
    r = owner_client.post(wurl(org, wf["id"], "versions"), {"graph": {"steps": [NOTIFY, {
        "key": "bye", "kind": "notify", "config": {"title": "Bye"}, "depends_on": ["hello"]}]}}, format="json")
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["published_at"] is None
    versions = owner_client.get(wurl(org, wf["id"], "versions")).json()["items"]
    assert [v["version"] for v in versions] == [2, 1]
    assert owner_client.get(wurl(org, wf["id"])).json()["current_version"] == 1  # draft v2 not live yet
    r = owner_client.post(wurl(org, wf["id"]), {}, format="json")
    assert r.status_code == 405
    assert owner_client.post(wurl(org), {"name": wf["name"]}, format="json").status_code == 409


# ------------------------------------------------------------------------------------------------ runner
def test_manual_run_with_condition_branches_templates_and_audit(org, owner, owner_client, run_now):
    steps = [
        {"key": "check", "kind": "condition", "config": {"left": "{{ input.amount }}", "op": "gt", "right": 100}},
        {"key": "big", "kind": "notify", "depends_on": ["check"], "config": {"title": "Big order {{ input.amount }}"}},
        {"key": "small", "kind": "notify", "depends_on": ["check"], "when": False, "config": {"title": "Small"}},
        {"key": "after_small", "kind": "notify", "depends_on": ["small"], "config": {"title": "never"}},
    ]
    wf = make_workflow(owner_client, org, steps)
    acc = start(owner_client, org, wf["id"], run_now, {"amount": 250})
    assert acc["status_url"].endswith(acc["run_id"]) and acc["events_url"]
    run = get_run(owner_client, org, acc["run_id"])
    assert run["status"] == "succeeded", run
    s = steps_by_key(run)
    assert s["check"]["output"] == {"result": True}
    assert s["big"]["status"] == "succeeded" and s["big"]["input"]["title"] == "Big order 250"
    assert s["small"]["status"] == "skipped" and s["after_small"]["status"] == "skipped"
    with tenant_context(org.id):
        assert Notification.objects.filter(user_id=owner.pk, title="Big order 250").exists()
        actions = set(AuditEvent.objects.filter(target_id__in=[acc["run_id"], *[x["id"] for x in run["steps"]]])
                      .values_list("action", flat=True))
        assert {"automation.run.create", "automation.step.succeeded", "automation.run.succeeded"} <= actions
        from apps.core.models import Job

        assert Job.objects.get(id=acc["job_id"]).status == "succeeded"
    runs = owner_client.get(org_url(org, "workflow-runs?status=succeeded")).json()
    assert [r["id"] for r in runs["items"]] == [acc["run_id"]] and "next_cursor" in runs


def test_inactive_workflow_cannot_run_and_viewer_cannot_start(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [NOTIFY], publish=False)
    r = owner_client.post(wurl(org, wf["id"], "runs"), {}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "workflow_not_active"
    owner_client.post(wurl(org, wf["id"], "publish"), {}, format="json")
    viewer = client_for(member_of(org, "viewer"))
    assert viewer.post(wurl(org, wf["id"], "runs"), {}, format="json").status_code == 403
    assert viewer.get(wurl(org)).status_code == 403  # viewer has no automation.view
    analyst = client_for(member_of(org, "analyst"))  # automation.view only
    assert analyst.get(wurl(org)).status_code == 200
    assert analyst.post(wurl(org), {"name": "x"}, format="json").status_code == 403


def test_retries_then_success_and_immediate_failure_without_retries(org, owner_client, run_now, test_steps):
    wf = make_workflow(owner_client, org, [{"key": "f", "kind": "test.flaky", "retries": 2,
                                            "config": {"fail_times": 2}}])
    run = get_run(owner_client, org, start(owner_client, org, wf["id"], run_now)["run_id"])
    f = steps_by_key(run)["f"]
    assert run["status"] == "succeeded" and f["attempt"] == 3 and len(f["history"]) == 2
    test_steps["calls"] = 0
    wf2 = make_workflow(owner_client, org, [{"key": "f", "kind": "test.flaky", "config": {"fail_times": 1}},
                                            {"key": "n", "kind": "notify", "depends_on": ["f"],
                                             "config": {"title": "x"}}], name="no retries")
    run = get_run(owner_client, org, start(owner_client, org, wf2["id"], run_now)["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "flaky" and run["error"]["step"] == "f"
    assert steps_by_key(run)["n"]["status"] == "cancelled"


def test_retry_backoff_waits_for_the_dispatcher(org, owner_client, run_now, test_steps):
    wf = make_workflow(owner_client, org, [{"key": "f", "kind": "test.flaky", "retries": 1, "retry_backoff_s": 30,
                                            "config": {"fail_times": 1}}])
    run_id = start(owner_client, org, wf["id"], run_now)["run_id"]
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "waiting" and steps_by_key(run)["f"]["status"] == "retrying"
    with tenant_context(org.id):
        assert run_now(services.process_due_for_current_org, timezone.now())["resumed"] == 0  # not due yet
        assert run_now(services.process_due_for_current_org,
                       timezone.now() + timedelta(seconds=31))["resumed"] == 1
    assert get_run(owner_client, org, run_id)["status"] == "succeeded"


def test_step_timeout_is_enforced(org, owner_client, run_now, test_steps):
    wf = make_workflow(owner_client, org, [{"key": "s", "kind": "test.slow", "timeout_s": 1,
                                            "config": {"seconds": 3}}])
    t0 = time.monotonic()
    run = get_run(owner_client, org, start(owner_client, org, wf["id"], run_now)["run_id"])
    assert time.monotonic() - t0 < 3
    assert run["status"] == "failed" and run["error"]["code"] == "step_timeout"
    with connection.cursor() as cur:  # the step's timeout never leaks into the surrounding transaction
        cur.execute("SELECT pg_sleep(1.2)")


def test_wait_step_pauses_until_due(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [{"key": "w", "kind": "wait", "config": {"seconds": 60}},
                                           {"key": "n", "kind": "notify", "depends_on": ["w"],
                                            "config": {"title": "after the wait"}}])
    run_id = start(owner_client, org, wf["id"], run_now)["run_id"]
    assert get_run(owner_client, org, run_id)["status"] == "waiting"
    with tenant_context(org.id):
        run_now(services.process_due_for_current_org, timezone.now() + timedelta(seconds=61))
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "succeeded" and steps_by_key(run)["n"]["status"] == "succeeded"


def test_step_permission_is_rechecked_as_the_actor(org, run_now, test_steps, owner_client):
    """A member allowed to run workflows but not pipelines cannot reach a pipelines step through a workflow."""
    wf = make_workflow(owner_client, org, [{"key": "p", "kind": "test.pipeline", "config": {}}])
    runner_user = member_with_permissions(org, ["automation.view", "automation.run"])
    c = client_for(runner_user)
    run = get_run(c, org, start(c, org, wf["id"], run_now)["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "step_permission_denied"
    assert steps_by_key(run)["p"]["output"] is None
    # the owner (who holds pipelines.run) can
    run = get_run(owner_client, org, start(owner_client, org, wf["id"], run_now)["run_id"])
    assert run["status"] == "succeeded"


def test_actor_removed_while_waiting_fails_the_run(org, owner_client, run_now):
    member = member_with_permissions(org, ["automation.view", "automation.run"])
    wf = make_workflow(owner_client, org, [{"key": "w", "kind": "wait", "config": {"seconds": 5}},
                                           {"key": "n", "kind": "notify", "depends_on": ["w"],
                                            "config": {"title": "x"}}])
    run_id = start(client_for(member), org, wf["id"], run_now)["run_id"]
    from apps.orgs.models import Membership

    with tenant_context(org.id):
        Membership.objects.filter(user=member).update(status="suspended")
        run_now(services.process_due_for_current_org, timezone.now() + timedelta(seconds=6))
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "failed" and run["error"]["code"] == "actor_not_member"


def test_outputs_are_redacted_for_other_members(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [NOTIFY])
    run_id = start(owner_client, org, wf["id"], run_now, {"name": "Synthetic"})["run_id"]
    analyst = client_for(member_of(org, "analyst"))
    run = get_run(analyst, org, run_id)
    assert run["output_redacted"] is True and run["steps"][0]["output"] is None and run["input"] is None
    assert get_run(owner_client, org, run_id)["steps"][0]["output"]["sent"] == 1


def test_cancel_run_cancels_pending_approval(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [{"key": "ok", "kind": "approval", "config": {"title": "Go?"}}])
    run_id = start(owner_client, org, wf["id"], run_now)["run_id"]
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "waiting_approval"
    r = owner_client.post(org_url(org, f"workflow-runs/{run_id}/cancel"), {}, format="json")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    with tenant_context(org.id):
        assert ApprovalRequest.objects.get(id=run["steps"][0]["approval_id"]).status == "cancelled"


# ------------------------------------------------------------------------------------------------ approvals
def approval_flow(owner_client, org, run_now, *, impact="low", perm=None):
    config = {"title": "Ship it?", "impact": impact}
    if perm:
        config["approver_permission"] = perm
    wf = make_workflow(owner_client, org, [{"key": "ok", "kind": "approval", "config": config},
                                           {"key": "n", "kind": "notify", "depends_on": ["ok"],
                                            "config": {"title": "approved by {{ steps.ok.output.decided_by }}"}}],
                       name=f"approval {impact} {uuid4().hex[:6]}")
    run_id = start(owner_client, org, wf["id"], run_now)["run_id"]
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "waiting_approval"
    return run_id, run["steps"][0]["approval_id"]


def decision_url(org, approval_id):
    return org_url(org, f"approvals/{approval_id}/decision")


def test_approval_inbox_and_decision_resumes_the_run(org, owner, owner_client, run_now):
    run_id, approval_id = approval_flow(owner_client, org, run_now)
    inbox = owner_client.get(org_url(org, "approvals?status=pending")).json()["items"]
    assert [a["id"] for a in inbox] == [approval_id] and inbox[0]["requires_reauth"] is False
    detail = owner_client.get(org_url(org, f"approvals/{approval_id}")).json()
    assert detail["can_decide"] is True and detail["payload"]["run_id"] == run_id
    analyst = client_for(member_of(org, "analyst"))  # no approvals.decide
    assert analyst.get(org_url(org, "approvals")).json()["items"] == []
    assert analyst.get(org_url(org, f"approvals/{approval_id}")).status_code == 404
    assert analyst.post(decision_url(org, approval_id), {"decision": "approve"}, format="json").status_code == 404
    r = run_now(owner_client.post, decision_url(org, approval_id), {"decision": "approve", "comment": "ok"},
                format="json")
    assert r.status_code == 200, r.content
    assert r.json()["status"] == "approved" and r.json()["decided_by"] == str(owner.pk)
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "succeeded"
    assert steps_by_key(run)["n"]["input"]["title"] == f"approved by {owner.pk}"
    r = owner_client.post(decision_url(org, approval_id), {"decision": "reject"}, format="json")
    assert r.status_code == 409 and r.json()["code"] == "approval_not_pending"


def test_rejection_fails_the_run(org, owner_client, run_now):
    run_id, approval_id = approval_flow(owner_client, org, run_now)
    r = run_now(owner_client.post, decision_url(org, approval_id), {"decision": "reject"}, format="json")
    assert r.status_code == 200
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "failed" and run["error"]["code"] == "approval_rejected"
    assert steps_by_key(run)["n"]["status"] == "cancelled"


def test_high_impact_decision_requires_password_reauth(org, owner_client, run_now):
    run_id, approval_id = approval_flow(owner_client, org, run_now, impact="high")
    for body in ({"decision": "approve"}, {"decision": "approve", "password": "wrong-password"}):
        r = owner_client.post(decision_url(org, approval_id), body, format="json")
        assert r.status_code == 400 and r.json()["code"] == "reauth_failed", r.content
        assert r["Content-Type"].startswith("application/problem+json")
    with tenant_context(org.id):
        assert ApprovalRequest.objects.get(id=approval_id).status == "pending"
    r = run_now(owner_client.post, decision_url(org, approval_id),
                {"decision": "approve", "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 200, r.content
    assert get_run(owner_client, org, run_id)["status"] == "succeeded"


def test_api_keys_cannot_decide():
    from apps.automation.views import ApprovalDecisionView
    from apps.core.permissions import DenyApiKey

    assert DenyApiKey in ApprovalDecisionView.permission_classes

    class KeyRequest:
        _nexora_api_key_id = uuid4()

    assert DenyApiKey().has_permission(KeyRequest(), None) is False


def test_expired_approval_fails_the_run(org, owner_client, run_now):
    run_id, approval_id = approval_flow(owner_client, org, run_now)
    with tenant_context(org.id):
        ApprovalRequest.objects.filter(id=approval_id).update(expires_at=timezone.now() - timedelta(seconds=1))
    run_now(services.dispatch_due)
    run = get_run(owner_client, org, run_id)
    assert run["status"] == "failed" and run["error"]["code"] == "approval_expired"


def test_model_promotion_approval_calls_the_ml_hook(org, owner, owner_client, monkeypatch):
    import apps.ml.services as ml_services

    applied = []
    monkeypatch.setattr(ml_services, "apply_promotion", lambda a: applied.append(a.id) or {"promoted": True},
                        raising=False)
    with tenant_context(org.id):
        a = services.request_approval(kind="model_promotion", title="Promote churn v3 to production",
                                      payload={"model_id": str(uuid4()), "version": 3},
                                      approver_permission="ml.promote", impact="high", requested_by=owner.pk)
    r = owner_client.post(decision_url(org, a.id), {"decision": "approve", "password": DEFAULT_PASSWORD},
                          format="json")
    assert r.status_code == 200, r.content
    assert applied == [a.id] and r.json()["result"] == {"promoted": True}

    def broken(approval):
        raise RuntimeError("boom")

    monkeypatch.setattr(ml_services, "apply_promotion", broken, raising=False)
    with tenant_context(org.id):
        b = services.request_approval(kind="model_promotion", title="Promote v4", payload={},
                                      approver_permission="ml.promote", impact="high", requested_by=owner.pk)
    r = owner_client.post(decision_url(org, b.id), {"decision": "approve", "password": DEFAULT_PASSWORD},
                          format="json")
    assert r.status_code == 422 and r.json()["code"] == "approval_apply_failed"
    with tenant_context(org.id):
        assert ApprovalRequest.objects.get(id=b.id).status == "pending"  # nothing half-applied


# ------------------------------------------------------------------------------------------------ webhooks
def make_webhook(owner_client, org, wf_id) -> dict:
    r = owner_client.post(wurl(org, wf_id, "triggers"), {"type": "webhook"}, format="json")
    assert r.status_code == 201, r.content
    t = r.json()
    assert t["secret"].startswith("whsec_") and t["webhook_url"] == f"/api/v1/hooks/{t['id']}"
    return t


def post_hook(client, url, body: bytes, *, secret: str, ts: int | None = None, signature: str | None = None):
    ts = int(time.time()) if ts is None else ts
    sig = signature or services.sign_webhook(secret, ts, body)
    return client.post(url, data=body, content_type="application/json",
                       HTTP_X_NEXORA_TIMESTAMP=str(ts), HTTP_X_NEXORA_SIGNATURE=sig)


def test_webhook_hmac_replay_and_forgery(org, owner_client, anon_client, run_now):
    wf = make_workflow(owner_client, org, [NOTIFY])
    t = make_webhook(owner_client, org, wf["id"])
    secret, url = t["secret"], t["webhook_url"]
    assert "secret" not in owner_client.get(wurl(org, wf["id"], "triggers")).json()["items"][0]  # shown once
    body = json.dumps({"name": "Webhook"}).encode()
    ts = int(time.time())
    r = run_now(post_hook, anon_client, url, body, secret=secret, ts=ts)
    assert r.status_code == 202, r.content
    run = get_run(owner_client, org, r.json()["run_id"])
    assert run["trigger_type"] == "webhook" and run["status"] == "succeeded"
    # replay of the identical signed request
    r = post_hook(anon_client, url, body, secret=secret, ts=ts)
    assert r.status_code == 409 and r.json()["code"] == "webhook_replayed"
    assert r["Content-Type"].startswith("application/problem+json")
    # forged: wrong secret, tampered body, missing signature
    for kwargs in ({"secret": "whsec_forged"}, {"secret": secret, "signature": "sha256=" + "0" * 64}):
        r = post_hook(anon_client, url, body, **kwargs)
        assert r.status_code == 401 and r.json()["code"] == "webhook_signature_invalid"
    good_sig = services.sign_webhook(secret, ts, body)
    r = post_hook(anon_client, url, b'{"name": "tampered"}', secret=secret, ts=ts, signature=good_sig)
    assert r.status_code == 401
    # stale (older than 5 minutes) and future timestamps
    for skew in (-301, 400):
        r = post_hook(anon_client, url, body, secret=secret, ts=int(time.time()) + skew)
        assert r.status_code == 401 and r.json()["code"] == "webhook_stale"
    r = anon_client.post(url, data=body, content_type="application/json")
    assert r.status_code == 401
    # unknown trigger -> 404, rotated secret invalidates the old one
    assert post_hook(anon_client, f"/api/v1/hooks/{uuid4()}", body, secret=secret).status_code == 404
    rot = owner_client.post(wurl(org, wf["id"], "triggers", t["id"], "rotate-secret"), {}, format="json").json()
    assert post_hook(anon_client, url, b'{"n": 1}', secret=secret).status_code == 401
    assert run_now(post_hook, anon_client, url, b'{"n": 1}', secret=rot["secret"]).status_code == 202
    # deactivated -> 404
    owner_client.patch(wurl(org, wf["id"], "triggers", t["id"]), {"is_active": False}, format="json")
    assert post_hook(anon_client, url, b'{"n": 2}', secret=rot["secret"]).status_code == 404
    with tenant_context(org.id):
        assert WebhookDelivery.objects.filter(trigger_id=t["id"]).count() == 2


def test_webhook_of_another_org_cannot_be_used_with_its_own_secret(org, other_org, owner_client, anon_client,
                                                                   run_now):
    wf = make_workflow(owner_client, org, [NOTIFY])
    make_webhook(owner_client, org, wf["id"])
    other = client_for(other_org_owner(other_org))
    r = other.get(wurl(other_org, wf["id"], "triggers"))
    assert r.status_code == 404


def other_org_owner(other_org):
    from apps.orgs.models import Membership

    with tenant_context(other_org.id):
        return Membership.objects.select_related("user").get(role__key="owner").user


# ------------------------------------------------------------------------------------------------ schedule + event
def test_schedule_trigger_fires_from_the_dispatcher(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [NOTIFY])
    bad = owner_client.post(wurl(org, wf["id"], "triggers"), {"type": "schedule", "config": {"cron": "nope"}},
                            format="json")
    assert bad.status_code == 400
    r = owner_client.post(wurl(org, wf["id"], "triggers"), {"type": "schedule", "config": {"cron": "*/5 * * * *"}},
                          format="json")
    assert r.status_code == 201 and r.json()["next_run_at"]
    with tenant_context(org.id):
        WorkflowTrigger.objects.filter(id=r.json()["id"]).update(next_run_at=timezone.now() - timedelta(seconds=1))
    summary = run_now(services.dispatch_due)
    assert summary[str(org.id)]["schedules"] == 1
    with tenant_context(org.id):
        run = WorkflowRun.objects.get(trigger_id=r.json()["id"])
        assert run.trigger_type == "schedule" and run.status == "succeeded"
        assert WorkflowTrigger.objects.get(id=r.json()["id"]).next_run_at > timezone.now()
    assert str(org.id) not in run_now(services.dispatch_due)  # nothing due any more


def test_event_trigger_matches_kind_and_filter(org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [NOTIFY])
    r = owner_client.post(wurl(org, wf["id"], "triggers"), {"type": "event", "config": {
        "event": "pipeline.run_failed", "filter": {"pipeline_id": "p-1"}}}, format="json")
    assert r.status_code == 201
    assert owner_client.post(wurl(org, wf["id"], "triggers"), {"type": "event", "config": {"event": "nope"}},
                             format="json").status_code == 400
    with tenant_context(org.id):
        assert run_now(emit, "pipeline.run_failed", {"pipeline_id": "p-2"}) == 0
        assert run_now(emit, "pipeline.run_succeeded", {"pipeline_id": "p-1"}) == 0
        assert run_now(emit, "pipeline.run_failed", {"pipeline_id": "p-1", "run_id": "r-1"}) == 1
        run = WorkflowRun.objects.get(trigger_type="event")
        assert run.status == "succeeded" and run.context["trigger"]["payload"]["run_id"] == "r-1"
    assert emit("pipeline.run_failed", {"pipeline_id": "p-1"}) == 0  # no tenant context: no-op, never raises
    assert run_now(emit, "pipeline.run_failed", {"pipeline_id": "p-1"}, org_id=org.id) == 1


# ------------------------------------------------------------------------------------------------ agents
def make_agent(owner_client, org, **extra) -> dict:
    body = {"name": "Ops helper", "allowed_tools": ["notify", "test.danger"], **extra}
    r = owner_client.post(org_url(org, "agents"), body, format="json")
    assert r.status_code == 201, r.content
    return r.json()


def test_agent_config_validation(org, owner_client, test_steps):
    bad = [{"name": "a", "allowed_tools": ["nope"]}, {"name": "b", "allowed_tools": []},
           {"name": "c", "allowed_tools": ["approval"]},
           {"name": "d", "allowed_tools": ["notify"], "tool_scopes": {"test.danger": {"target": ["x"]}}},
           {"name": "e", "allowed_tools": ["notify"], "max_steps": 99}]
    for body in bad:
        assert owner_client.post(org_url(org, "agents"), body, format="json").status_code == 400, body
    agent = make_agent(owner_client, org, tool_scopes={"test.danger": {"target": ["staging"]}})
    assert agent["approval_required_for"] == ["high"]
    r = owner_client.patch(org_url(org, f"agents/{agent['id']}"), {"allowed_tools": ["notify"]}, format="json")
    assert r.status_code == 200 and r.json()["tool_scopes"] == {}
    assert owner_client.post(org_url(org, "agents"), {"name": "Ops helper", "allowed_tools": ["notify"]},
                             format="json").status_code == 409


def test_agent_run_inserts_approval_before_high_impact_tools(org, owner_client, run_now, test_steps):
    agent = make_agent(owner_client, org, tool_scopes={"test.danger": {"target": ["staging"]}})
    url = org_url(org, f"agents/{agent['id']}/runs")
    for steps, code in (
        ([{"tool": "wait", "config": {"seconds": 1}}], "tool_not_allowed"),
        ([{"tool": "test.danger", "config": {"target": "production"}}], "out_of_scope"),
        ([{"tool": "notify", "config": {}}], "invalid_config"),
        ([{"tool": "notify", "config": {"title": "x"}}] * 9, "too_many_steps"),
    ):
        r = owner_client.post(url, {"steps": steps}, format="json")
        assert r.status_code == 422 and r.json()["code"] == "invalid_agent_plan", r.content
        assert code in {e["code"] for e in r.json()["errors"]}
    r = run_now(owner_client.post, url, {"goal": "tidy staging", "steps": [
        {"tool": "notify", "config": {"title": "starting"}},
        {"tool": "test.danger", "config": {"target": "staging"}}]}, format="json")
    assert r.status_code == 202, r.content
    run = get_run(owner_client, org, r.json()["run_id"])
    assert [s["step_key"] for s in run["steps"]] == ["tool_1", "approve_2", "tool_2"]
    assert run["status"] == "waiting_approval" and run["agent_id"] == agent["id"]
    approval_id = steps_by_key(run)["approve_2"]["approval_id"]
    with tenant_context(org.id):
        a = ApprovalRequest.objects.get(id=approval_id)
        assert a.kind == "agent_action" and a.impact == "high" and a.payload["details"]["tool"] == "test.danger"
    r = run_now(owner_client.post, decision_url(org, approval_id),
                {"decision": "approve", "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 200
    run = get_run(owner_client, org, run["id"])
    assert run["status"] == "succeeded" and steps_by_key(run)["tool_2"]["output"] == {"done": True,
                                                                                   "target": "staging"}
    with tenant_context(org.id):
        assert AuditEvent.objects.filter(action="automation.agent.run", target_id=agent["id"]).exists()


def test_agent_planning_uses_gemini_with_schema_only(org, owner_client, run_now, test_steps):
    from apps.ai import services as ai_services

    agent = make_agent(owner_client, org)
    seen = {}

    class FakeEngine:
        def generate_structured(self, **kwargs):
            seen.update(kwargs)
            return {"steps": [{"tool": "notify", "config_json": json.dumps({"title": "Planned"})}]}

    with ai_services.override_gemini(FakeEngine()):
        r = run_now(owner_client.post, org_url(org, f"agents/{agent['id']}/runs"), {"goal": "say hi"},
                    format="json")
    assert r.status_code == 202, r.content
    from nexora.contracts import PayloadClass

    assert set(seen["payload"].items) == {PayloadClass.SCHEMA} and seen["payload"].items[PayloadClass.SCHEMA] == ()
    assert "say hi" in seen["prompt"] and seen["prompt_key"] == "automation.agent_plan"
    run = get_run(owner_client, org, r.json()["run_id"])
    assert run["status"] == "succeeded" and run["input"]["planned_by"] == "gemini"


# ------------------------------------------------------------------------------------------------ bi step
def test_bi_saved_query_step_runs_under_the_actors_policy(org, owner, owner_client, run_now, monkeypatch):
    from apps.bi.models import BiQueryAudit, SavedQuery
    from apps.bi.tests.fakes import DATASET_ID, install

    install(monkeypatch)
    with tenant_context(org.id):
        sq = SavedQuery.objects.create(owner_id=owner.pk, dataset_id=DATASET_ID, name="Revenue",
                                       sql="SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region")
    wf = make_workflow(owner_client, org, [
        {"key": "q", "kind": "bi.saved_query", "config": {"saved_query_id": str(sq.id), "include_rows": True,
                                                          "max_rows": 2}},
        {"key": "many", "kind": "condition", "depends_on": ["q"],
         "config": {"left": "{{ steps.q.output.row_count }}", "op": "gte", "right": 1}}])
    run = get_run(owner_client, org, start(owner_client, org, wf["id"], run_now)["run_id"])
    assert run["status"] == "succeeded", run
    out = steps_by_key(run)["q"]["output"]
    assert out["row_count"] >= 1 and len(out["rows"]) <= 2 and out["query_audit_id"]
    assert steps_by_key(run)["many"]["output"] == {"result": True}
    with tenant_context(org.id):
        assert BiQueryAudit.objects.get(id=out["query_audit_id"]).user_id == owner.pk
    # a private saved query of someone else is not visible to the run's actor
    other = member_with_permissions(org, ["automation.view", "automation.run", "dashboards.view"])
    run = get_run(client_for(other), org, start(client_for(other), org, wf["id"], run_now)["run_id"])
    assert run["status"] == "failed" and run["error"]["code"] == "not_found"


# ------------------------------------------------------------------------------------------------ isolation
def test_cross_org_resources_are_404(org, other_org, owner_client, run_now):
    wf = make_workflow(owner_client, org, [{"key": "ok", "kind": "approval", "config": {"title": "Go?"}}])
    run_id = start(owner_client, org, wf["id"], run_now)["run_id"]
    approval_id = get_run(owner_client, org, run_id)["steps"][0]["approval_id"]
    agent = make_agent(owner_client, org, allowed_tools=["notify"])
    other = client_for(other_org_owner(other_org))
    for path in (f"workflows/{wf['id']}", f"workflows/{wf['id']}/versions", f"workflows/{wf['id']}/triggers",
                 f"workflow-runs/{run_id}", f"workflow-runs/{run_id}/steps", f"agents/{agent['id']}",
                 f"approvals/{approval_id}"):
        assert other.get(org_url(other_org, path)).status_code == 404, path
        assert other.get(org_url(org, path)).status_code == 404, path  # not a member of org A
    assert other.post(org_url(other_org, f"approvals/{approval_id}/decision"), {"decision": "approve"},
                      format="json").status_code == 404
    assert other.post(org_url(other_org, f"workflows/{wf['id']}/runs"), {}, format="json").status_code == 404
    with tenant_context(other_org.id):
        assert not Workflow.objects.exists() and not WorkflowStepRun.objects.exists()
        assert not AgentConfig.objects.exists()


def test_step_catalog_lists_builtins_and_bi(org, owner_client):
    items = {s["kind"]: s for s in owner_client.get(org_url(org, "automation/steps")).json()["items"]}
    assert {"condition", "notify", "approval", "wait", "bi.saved_query"} <= set(items)
    assert items["bi.saved_query"]["permission"] == "dashboards.view" and items["approval"]["schema"]


def test_problem_json_content_type_on_drf_errors(org, owner_client, anon_client):
    r = owner_client.get(org_url(org, f"workflows/{uuid4()}"))
    assert r.status_code == 404 and r["Content-Type"] == "application/problem+json"
    r = anon_client.get(org_url(org, "workflows"))
    assert r.status_code in (401, 403) and r["Content-Type"] == "application/problem+json"
