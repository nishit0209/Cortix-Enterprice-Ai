"""W5 ML: production promotion through an approval (202 + approval_id, applied by `apply_promotion`), the
automation step kinds `ml.train` / `ml.batch_predict` (synchronous, permission re-check) and the Customer 360
recipe (churn + CLV projects from customers + orders). Real database, in-memory object storage."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from rest_framework.exceptions import PermissionDenied

from apps.ml import services, steps

from .test_api_db import _registered

pytestmark = pytest.mark.django_db(transaction=False)


@pytest.fixture
def approvals(monkeypatch, settings):
    """Stands in for apps.automation.services.request_approval, so the ML side is tested on its own."""
    settings.ML_PRODUCTION_REQUIRES_APPROVAL = True
    created = []

    def request_approval(*, kind, title, payload, approver_permission, impact, requested_by):
        approval = SimpleNamespace(id=uuid4(), kind=kind, title=title, payload=payload, status="pending",
                                   approver_permission=approver_permission, impact=impact,
                                   requested_by_id=requested_by, decided_by_id=None)
        created.append(approval)
        return approval

    monkeypatch.setattr(services, "_request_approval_fn", lambda: request_approval)
    return created


def _stages(env, model_id) -> dict:
    return {v["version"]: v["stage"] for v in env.owner.get(f"{env.base}/models/{model_id}").json()["versions"]}


def test_production_promotion_creates_an_approval_and_applies_on_grant(env, approvals):
    _project, _exp, reg = _registered(env)
    model_id = reg["model_id"]
    staging = env.owner.post(f"{env.base}/models/{model_id}/versions/1/promote", {"stage": "staging"}, format="json")
    assert staging.status_code == 200 and staging.json()["stage"] == "staging"  # staging stays immediate
    assert approvals == []

    resp = env.owner.post(f"{env.base}/models/{model_id}/versions/1/promote", {"stage": "production"},
                          format="json")
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["status"] == "pending_approval" and body["approval_id"] == str(approvals[0].id)
    approval = approvals[0]
    assert approval.kind == "model_promotion" and approval.impact == "high"
    assert approval.approver_permission == "ml.promote" and approval.requested_by_id == env.a.owner.pk
    assert approval.payload["model_id"] == model_id and approval.payload["version"] == 1
    assert approval.payload["hook"] == "apps.ml.services.apply_promotion"
    assert _stages(env, model_id) == {1: "staging"}  # nothing changes before the decision

    with env.a.tenant():
        rejected = SimpleNamespace(**{**vars(approval), "status": "rejected"})
        assert services.apply_promotion(rejected) is None
    assert _stages(env, model_id) == {1: "staging"}

    with env.a.tenant():
        granted = SimpleNamespace(**{**vars(approval), "status": "approved", "decided_by_id": env.a.owner.pk})
        version = services.apply_promotion(granted)
    assert version.stage == "production" and version.promoted_by == env.a.owner.pk
    assert _stages(env, model_id) == {1: "production"}

    from apps.core.models import AuditEvent

    with env.a.tenant():
        actions = list(AuditEvent.objects.filter(action__startswith="ml.model.prom").values_list("action",
                                                                                               "metadata"))
    assert ("ml.model.promotion_requested" in {a for a, _ in actions})
    assert any(a == "ml.model.promote" and m.get("approval_id") == str(approval.id) for a, m in actions)


def test_promotion_without_ml_promote_is_403_and_creates_no_approval(env, approvals):
    _project, _exp, reg = _registered(env)
    analyst = env.client_for(env.a.analyst)
    resp = analyst.post(f"{env.base}/models/{reg['model_id']}/versions/1/promote", {"stage": "production"},
                        format="json")
    assert resp.status_code == 403 and approvals == []


def _ctx(user):
    return SimpleNamespace(actor_id=user.pk, org_id=None, run=None)


def test_train_step_trains_registers_and_requests_production(env, approvals):
    project = env.owner.post(f"{env.base}/projects", {"name": "Churn auto", "dataset_id": env.dataset_id,
                                                      "target": "churned"}, format="json").json()
    with env.a.tenant():
        out = steps.train_step(_ctx(env.a.owner), {"project_id": project["id"], "algorithms": ["logistic_regression"],
                                                   "random_state": 5, "promote_to": "production",
                                                   "model_name": "Churn (automated)"})
    assert out["best_experiment_id"] and out["experiments"][0]["status"] == "succeeded"
    assert out["version"] == 1 and out["stage"] == "pending_approval" and out["approval_id"]
    assert _stages(env, out["model_id"]) == {1: "none"}

    with env.a.tenant():
        staged = steps.train_step(_ctx(env.a.owner), {"project_id": project["id"],
                                                      "algorithms": ["logistic_regression"],
                                                      "promote_to": "staging", "model_name": "Churn (automated)"})
    assert staged["version"] == 2 and staged["stage"] == "staging"


def test_steps_recheck_the_actor_permissions(env):
    project = env.owner.post(f"{env.base}/projects", {"name": "Churn perms", "dataset_id": env.dataset_id,
                                                      "target": "churned"}, format="json").json()
    with env.a.tenant(), pytest.raises(PermissionDenied):
        steps.train_step(_ctx(env.a.viewer), {"project_id": project["id"]})
    with env.a.tenant(), pytest.raises(PermissionDenied):  # analyst: ml.manage but not ml.promote
        steps.train_step(_ctx(env.a.analyst), {"project_id": project["id"], "promote_to": "staging"})
    with env.a.tenant(), pytest.raises(PermissionDenied):  # a member of ANOTHER org
        steps.batch_predict_step(_ctx(env.b.owner), {"model_id": str(uuid4()), "dataset_id": env.dataset_id})
    from apps.ml.models import Experiment

    with env.a.tenant():
        assert not Experiment.objects.filter(project_id=project["id"]).exists()


def test_batch_predict_step_publishes_predictions(env, settings):
    settings.ML_PRODUCTION_REQUIRES_APPROVAL = False
    _project, _exp, reg = _registered(env)
    env.owner.post(f"{env.base}/models/{reg['model_id']}/versions/1/promote", {"stage": "production"},
                   format="json")
    with env.a.tenant():
        out = steps.batch_predict_step(_ctx(env.a.owner), {"model_id": reg["model_id"],
                                                           "dataset_id": env.dataset_id,
                                                           "output_name": "Churn scores (automated)"})
    assert out["job_id"] and out["rows"] == 240 and out["version"] == 1
    from apps.core.models import Job

    with env.a.tenant():
        assert Job.objects.get(id=out["job_id"]).status == "succeeded"


def test_register_steps_uses_the_automation_registry(monkeypatch):
    import sys
    import types

    registered = {}
    fake = types.ModuleType("apps.automation.registry")
    fake.register_step = lambda kind, handler, *, schema, impact="low": registered.update({kind: (handler, schema,
                                                                                                  impact)})
    monkeypatch.setitem(sys.modules, "apps.automation.registry", fake)
    steps.register_steps()
    from apps.documents import steps as doc_steps

    doc_steps.register_steps()
    assert set(registered) == {"ml.train", "ml.batch_predict", "documents.process"}
    assert registered["ml.train"][1]["required"] == ["project_id"]
