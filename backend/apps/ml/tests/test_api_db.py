"""ML workspace API against the real database: projects, EDA, experiments (real CPU training), registry, promote,
predict (session AND API key), batch predictions, monitoring, permissions, cross-org 404 and policy exclusion.

The dataset is ingested through the real upload path (synthetic churn data); object storage is in memory."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db(transaction=False)


def _project(env, **extra):
    body = {"name": "Churn", "dataset_id": env.dataset_id, "target": "churned", **extra}
    resp = env.owner.post(f"{env.base}/projects", body, format="json")
    assert resp.status_code == 201, resp.content
    return resp.json()


def _train(env, project_id, algorithm="logistic_regression"):
    with env.capture(execute=True):
        resp = env.owner.post(f"{env.base}/projects/{project_id}/experiments",
                              {"algorithm": algorithm, "random_state": 3}, format="json")
    assert resp.status_code == 202, resp.content
    exp = env.owner.get(f"{env.base}/experiments/{resp.json()['experiment_id']}").json()
    assert exp["status"] == "succeeded", exp
    return exp


def _registered(env):
    project = _project(env)
    exp = _train(env, project["id"])
    reg = env.owner.post(f"{env.base}/experiments/{exp['id']}/register", {"name": "Churn model"}, format="json")
    assert reg.status_code == 201, reg.content
    return project, exp, reg.json()


ROW = {"age": 40, "plan": "basic", "monthly_spend": 55.5, "tenure_months": 3}


def test_project_eda_recommendations_and_leakage(env):
    project = _project(env)
    assert project["task_type"] == "classification" and project["task_type_source"] == "auto"
    assert "customer_id" in {d["column"] for d in project["leakage"]}
    assert "customer_id" not in project["features"] and "tenure_months" in project["features"]

    assert env.owner.get(f"{env.base}/projects/{project['id']}/eda").status_code == 404
    with env.capture(execute=True):
        run = env.owner.post(f"{env.base}/projects/{project['id']}/eda", {}, format="json")
    assert run.status_code == 202 and run.json()["job_id"]
    eda = env.owner.get(f"{env.base}/projects/{project['id']}/eda")
    assert eda.status_code == 200 and eda.json()["status"] == "ready"
    recs = env.owner.get(f"{env.base}/projects/{project['id']}/recommendations").json()
    assert 2 <= len(recs["candidates"]) <= 4 and all(c["reasons"] for c in recs["candidates"])
    listed = env.owner.get(f"{env.base}/projects").json()
    assert project["id"] in {p["id"] for p in listed["items"]}


def test_train_compare_register_promote_predict_monitor(env, settings):
    settings.ML_PRODUCTION_REQUIRES_APPROVAL = False  # the W4 direct flow; approvals: test_w5_automation_db
    project = _project(env)
    lr = _train(env, project["id"], "logistic_regression")
    rf = _train(env, project["id"], "random_forest")
    assert 0.5 < lr["metrics"]["test"]["roc_auc"] <= 1.0
    detail = env.owner.get(f"{env.base}/experiments/{rf['id']}").json()
    assert detail["feature_importances"] and detail["artifact_sha256"]

    cmp = env.owner.get(f"{env.base}/experiments/compare", {"ids": f"{lr['id']},{rf['id']}"}).json()
    assert cmp["best_id"] in (lr["id"], rf["id"]) and len(cmp["experiments"]) == 2

    reg = env.owner.post(f"{env.base}/experiments/{lr['id']}/register", {"name": "Churn model"}, format="json")
    assert reg.status_code == 201, reg.content
    model_id = reg.json()["model_id"]
    # no staging/production version yet -> 409 model_not_ready
    assert env.owner.post(f"{env.base}/models/{model_id}/predict", {"rows": [ROW]}, format="json").status_code == 409

    reg2 = env.owner.post(f"{env.base}/experiments/{rf['id']}/register", {"model_id": model_id}, format="json")
    assert reg2.json()["version"] == 2
    for v in (1, 2):
        resp = env.owner.post(f"{env.base}/models/{model_id}/versions/{v}/promote", {"stage": "production"},
                              format="json")
        assert resp.status_code == 200 and resp.json()["stage"] == "production"
    stages = {v["version"]: v["stage"] for v in env.owner.get(f"{env.base}/models/{model_id}").json()["versions"]}
    assert stages == {1: "archived", 2: "production"}

    pred = env.owner.post(f"{env.base}/models/{model_id}/predict", {"rows": [ROW, {**ROW, "tenure_months": 50}],
                                                                    "explain": True}, format="json")
    assert pred.status_code == 200, pred.content
    body = pred.json()
    assert body["version"] == 2 and body["n_rows"] == 2
    assert all(0 <= p["probability"] <= 1 and p["explanation"] for p in body["predictions"])

    bad = env.owner.post(f"{env.base}/models/{model_id}/predict", {"rows": [{"age": 30}]}, format="json")
    assert bad.status_code == 400 and bad.json()["code"] == "prediction_input_invalid"

    mon = env.owner.get(f"{env.base}/models/{model_id}/monitoring")
    assert mon.status_code == 200, mon.content

    from apps.ml.models import PredictionLog

    with env.a.tenant():
        log = PredictionLog.objects.get(model_id=model_id)
    assert log.caller_type == "user" and log.n_rows == 2 and len(log.input_hash) == 64
    assert "55.5" not in str(log.feature_counts)  # binned counts only, never the raw inputs


def test_predict_with_api_key_is_org_bound(env):
    _project_, _exp, reg = _registered(env)
    model_id = reg["model_id"]
    env.owner.post(f"{env.base}/models/{model_id}/versions/1/promote", {"stage": "staging"}, format="json")
    from apps.accounts.services import create_api_key

    with env.a.tenant():
        _key, token = create_api_key(name="scoring", scopes=["ml.predict"], created_by=env.a.owner)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    ok = client.post(f"{env.base}/models/{model_id}/predict", {"rows": ROW}, format="json")
    assert ok.status_code == 200, ok.content
    assert client.get(f"{env.base}/models/{model_id}/monitoring").status_code == 403  # scope is ml.predict only
    other = client.post(f"/api/v1/orgs/{env.b.slug}/ml/models/{model_id}/predict", {"rows": ROW}, format="json")
    assert other.status_code == 404

    from apps.ml.models import PredictionLog

    with env.a.tenant():
        log = PredictionLog.objects.get(model_id=model_id)
    assert log.caller_type == "api_key" and log.api_key_prefix and log.source == "api"


def test_permissions_and_cross_org(env):
    project, _exp, reg = _registered(env)
    from apps.core.tests.fixtures import member_with_permissions

    viewer = env.client_for(env.a.viewer)  # ml.view only
    assert viewer.get(f"{env.base}/projects/{project['id']}").status_code == 200
    assert viewer.post(f"{env.base}/projects", {"name": "x", "dataset_id": env.dataset_id, "target": "churned"},
                       format="json").status_code == 403
    analyst = env.client_for(env.a.analyst)  # ml.manage, no ml.promote
    for stage in ("staging", "production"):
        assert analyst.post(f"{env.base}/models/{reg['model_id']}/versions/1/promote", {"stage": stage},
                            format="json").status_code == 403
    no_ml = env.client_for(member_with_permissions(env.a.org, {"org.view", "datasets.view"}))
    assert no_ml.get(f"{env.base}/projects").status_code == 403

    intruder = env.client_for(env.b.owner)
    for path in (f"/projects/{project['id']}", f"/models/{reg['model_id']}"):
        assert intruder.get(f"/api/v1/orgs/{env.b.slug}/ml{path}").status_code == 404
        assert intruder.get(f"{env.base}{path}").status_code == 404

    from apps.core.models import AuditEvent

    with env.a.tenant():
        actions = set(AuditEvent.objects.filter(action__startswith="ml.").values_list("action", flat=True))
    assert {"ml.project.create", "ml.experiment.create", "ml.model.register"} <= actions


def test_policy_excluded_columns_never_become_features(env):
    from apps.governance.models import ColumnPolicy

    with env.a.tenant():
        ColumnPolicy.objects.create(dataset_id=env.dataset_id, column_name="monthly_spend",
                                    principal_type="everyone", action="deny")
    denied = env.owner.post(f"{env.base}/projects", {"name": "x", "dataset_id": env.dataset_id, "target": "churned",
                                                     "features": ["age", "monthly_spend"]}, format="json")
    assert denied.status_code == 400 and "monthly_spend" in str(denied.json())
    project = _project(env)
    assert "monthly_spend" not in project["features"]
    assert {"column": "monthly_spend", "reason": "denied"} in project["excluded_by_policy"]
    exp = _train(env, project["id"])
    detail = env.owner.get(f"{env.base}/experiments/{exp['id']}").json()
    names = {f["name"] for f in detail["feature_schema"]}
    assert names and "monthly_spend" not in names


def test_batch_predictions_publish_a_dataset(env, monkeypatch):
    import apps.datasets.services as ds

    _project_, _exp, reg = _registered(env)
    model_id = reg["model_id"]
    # production promotion needs an approval (W5); batch scoring serves the staging version
    promoted = env.owner.post(f"{env.base}/models/{model_id}/versions/1/promote", {"stage": "staging"}, format="json")
    assert promoted.status_code == 200, promoted.content
    assert promoted.json()["stage"] == "staging"
    published = {}

    def fake_publish_frame(*, name, frame, user_id, classification="internal", attest=False, dataset_id=None,
                           description="", source="ml"):
        published.update(name=name, columns=list(frame.columns), rows=frame.height, source=source)
        return None

    monkeypatch.setattr(ds, "publish_frame", fake_publish_frame, raising=False)
    with env.capture(execute=True):
        resp = env.owner.post(f"{env.base}/models/{model_id}/batch-predictions",
                              {"dataset_id": env.dataset_id, "output_name": "Churn scores"}, format="json")
    assert resp.status_code == 202, resp.content
    assert published.get("name") == "Churn scores" and published["rows"] == 240
    assert "prediction" in published["columns"] and "probability" in published["columns"]
