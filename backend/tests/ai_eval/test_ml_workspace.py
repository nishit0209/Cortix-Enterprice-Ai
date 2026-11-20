"""ML workspace correctness (CONTRACT "W4 additions"; ML_ARCHITECTURE §1-§8). QA oracle, no live Gemini.

  * Policy: a model trained by a RESTRICTED user cannot use hidden columns. With a column `deny` on `nps` and a
    `mask_null` on `region` for the analyst, the analyst's project lists both in `excluded_by_policy`, never
    uses them as features or target, and the registered version's feature schema excludes them; the owner's
    project on the same dataset (no policy) does use them (control).
  * Leakage guard: ID-like columns (`customer_id`, the all-unique `row_no`) and target duplicates (`churn_flag` ==
    target, `churn_score_post` |corr| > 0.98) are dropped from the features and from the trained model.
  * Metric oracle: the reported test accuracy / F1 / precision / recall equal sklearn computed INDEPENDENTLY in the
    test from the experiment's confusion matrix; RMSE / MAE equal numpy over the reported test residuals.
  * Determinism: the same `random_state` on the same snapshot gives identical metrics; the seed is recorded.
  * Gemini: on the free tier ML never declares or sends row VALUES (EDA + training narration), checked with the
    fake GeminiService behind the REAL AIDataPolicy gate, and string canaries from the rows never leave.
  * `apps.ml.services.predict_frame` / `ModelNotReady` exist with the CONTRACT names and semantics.

Synthetic data only (testdata/generated/churn.csv). DB-backed; skips until `apps.ml` routes exist.
"""

from __future__ import annotations

import json
import math

import pytest

from testing import ml
from testing.client import api_client_for

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not ml.HAS_ML_ROUTES, reason=ml.ML_SKIP_REASON),
]

ROWS = 900
HIDDEN = {"nps", "region"}
LEAKY = {"customer_id", "row_no", "churn_flag", "churn_score_post"}


@pytest.fixture
def ml_env(local_fs_storage, gated_fake_gemini, fake_embedder, fake_redis, settings):
    settings.GEMINI_TIER = "free"
    gated_fake_gemini.respond("*", {"summary": "Synthetic narration."}, stream=["Synthetic narration."])
    return gated_fake_gemini


def _schema_columns(world, model_id: str, version) -> list[str]:
    v = ml.model_version(world, model_id, version)
    assert v is not None
    schema = next((getattr(v, f) for f in ("feature_schema", "features", "schema") if hasattr(v, f)), None)
    assert schema is not None, "ModelVersion must record its feature schema"
    return ml.column_names(schema)


def _policy(owner, slug: str, dataset_id: str, column: str, action: str, user) -> None:
    resp = owner.post(
        f"/api/v1/orgs/{slug}/datasets/{dataset_id}/column-policies",
        {"column_name": column, "principal_type": "user", "principal_id": str(user.pk), "action": action},
        format="json",
    )
    assert resp.status_code == 201, resp.content[:300]


# ------------------------------------------------------------------------------------------------ policy
def test_restricted_users_model_cannot_use_hidden_columns(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner, analyst = api_client_for(a.owner), api_client_for(a.analyst)
    capture = django_capture_on_commit_callbacks
    dataset_id = ml.ingest_frame(owner, a.slug, ml.churn_rows(ROWS), capture, name="Churn", filename="churn.csv")
    _policy(owner, a.slug, dataset_id, "nps", "deny", a.analyst)
    _policy(owner, a.slug, dataset_id, "region", "mask_null", a.analyst)

    control = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned", name="owner")
    assert HIDDEN <= set(ml.project_features(control)), "control: without a policy the owner may use nps/region"
    assert not HIDDEN & set(ml.excluded_by_policy(control))

    project = ml.create_project(analyst, a.slug, dataset_id=dataset_id, target="churned", name="analyst")
    assert HIDDEN <= set(ml.excluded_by_policy(project)), project
    assert not HIDDEN & set(ml.project_features(project)), project

    # asking for the hidden columns explicitly is refused or silently excluded, never honoured
    resp = analyst.post(
        f"/api/v1/orgs/{a.slug}/ml/projects",
        {"name": "explicit", "dataset_id": dataset_id, "target": "churned", "features": ["nps", "tenure_months"]},
        format="json",
    )
    if resp.status_code in (200, 201):
        assert "nps" not in ml.project_features(resp.json())
    else:
        assert resp.status_code in (400, 403, 422), resp.content[:300]
    for target in HIDDEN:
        resp = analyst.post(
            f"/api/v1/orgs/{a.slug}/ml/projects",
            {"name": f"target {target}", "dataset_id": dataset_id, "target": target},
            format="json",
        )
        assert resp.status_code in (400, 403, 422), f"a hidden column as target: {resp.status_code}"

    project_id = str(ml.pick(project, "id"))
    algorithm = ml.recommended(analyst, a.slug, project_id)[0]
    exp = ml.train(analyst, a.slug, project_id, capture, algorithm=algorithm)
    blob = json.dumps(exp, default=str)
    for column in HIDDEN:
        assert f'"{column}"' not in blob, f"the analyst's experiment mentions the hidden column {column!r}"
    model_id, version = ml.register(analyst, a.slug, str(ml.pick(exp, "id")))
    assert not HIDDEN & set(_schema_columns(a, model_id, version))


# ------------------------------------------------------------------------------------------------ leakage
def test_leakage_guard_drops_id_like_and_target_duplicate_columns(two_orgs, ml_env,
                                                                  django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    rows = ml.leaky_churn_rows(ROWS)
    dataset_id = ml.ingest_frame(owner, a.slug, rows, capture, name="Churn leaky", filename="churn_leaky.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned")
    features = set(ml.project_features(project))
    assert not LEAKY & features, f"leaky columns kept as features: {sorted(LEAKY & features)}"
    assert {"tenure_months", "monthly_spend_inr", "support_tickets", "days_since_login"} <= features
    dropped = set(ml.leakage_dropped(project))
    if dropped:  # when the guard reports what it dropped, every hazard is named
        assert LEAKY <= dropped, f"leakage report misses {sorted(LEAKY - dropped)}"
    project_id = str(ml.pick(project, "id"))
    exp = ml.train(owner, a.slug, project_id, capture, algorithm=ml.recommended(owner, a.slug, project_id)[0])
    model_id, version = ml.register(owner, a.slug, str(ml.pick(exp, "id")))
    assert not LEAKY & set(_schema_columns(a, model_id, version))
    acc = ml.metric(ml.holdout_metrics(exp), "accuracy")
    assert acc < 0.99, f"accuracy {acc}: a target duplicate reached the model"


# ------------------------------------------------------------------------------------------------ metric oracle
def _expand(labels, matrix) -> tuple[list, list]:
    y_true, y_pred = [], []
    for i, row in enumerate(matrix):
        for j, n in enumerate(row):
            y_true += [str(labels[i])] * n
            y_pred += [str(labels[j])] * n
    return y_true, y_pred


def test_classification_metrics_equal_sklearn_on_the_reported_confusion_matrix(
    two_orgs, ml_env, django_capture_on_commit_callbacks
):
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score

    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    dataset_id = ml.ingest_frame(owner, a.slug, ml.churn_rows(ROWS), capture, name="Churn", filename="churn.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned",
                                task_type="classification")
    project_id = str(ml.pick(project, "id"))
    for algorithm in ml.recommended(owner, a.slug, project_id)[:2]:
        exp = ml.train(owner, a.slug, project_id, capture, algorithm=algorithm, random_state=3)
        labels, matrix = ml.confusion(exp)
        y_true, y_pred = _expand(labels, matrix)
        n_test = len(y_true)
        assert 0.1 * ROWS <= n_test <= 0.4 * ROWS, f"{algorithm}: {n_test} test rows: metrics not on a held-out split"
        reported = ml.holdout_metrics(exp)
        checks = {"accuracy": accuracy_score(y_true, y_pred)}
        for avg in ("macro", "weighted"):
            checks[f"f1_{avg}"] = f1_score(y_true, y_pred, average=avg, zero_division=0)
            checks[f"precision_{avg}"] = precision_score(y_true, y_pred, average=avg, zero_division=0)
            checks[f"recall_{avg}"] = recall_score(y_true, y_pred, average=avg, zero_division=0)
        positive = str(labels[-1])
        binary = {
            "f1": f1_score(y_true, y_pred, pos_label=positive, zero_division=0),
            "precision": precision_score(y_true, y_pred, pos_label=positive, zero_division=0),
            "recall": recall_score(y_true, y_pred, pos_label=positive, zero_division=0),
        }
        compared = []
        for name, want in checks.items():
            if name in reported:
                assert math.isclose(float(reported[name]), want, abs_tol=1e-6), (algorithm, name, reported[name], want)
                compared.append(name)
        for name, want in binary.items():  # a bare "f1" is the positive-class (binary) or the macro score
            if name in reported:
                got = float(reported[name])
                assert math.isclose(got, want, abs_tol=1e-6) or math.isclose(got, checks[f"{name}_macro"],
                                                                            abs_tol=1e-6), (algorithm, name, got)
                compared.append(name)
        assert "accuracy" in compared and any(c.startswith("f1") for c in compared), (algorithm, sorted(reported))


def _residuals(exp: dict) -> list[float] | None:
    raw = ml.pick(exp, "residuals", "metrics.test.residuals", "details.residuals", default=None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "y_true" in raw and "y_pred" in raw:
            return [float(t) - float(p) for t, p in zip(raw["y_true"], raw["y_pred"], strict=True)]
        if "actual" in raw and "predicted" in raw:
            return [float(t) - float(p) for t, p in zip(raw["actual"], raw["predicted"], strict=True)]
        raw = raw.get("values") or raw.get("residuals")
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        out = []
        for r in raw:
            if "residual" in r:
                out.append(float(r["residual"]))
            else:
                t = ml.pick(r, "y_true", "actual")
                p = ml.pick(r, "y_pred", "predicted")
                out.append(float(t) - float(p))
        return out
    if isinstance(raw, list) and all(isinstance(v, int | float) for v in raw):
        return [float(v) for v in raw]
    return None


def test_regression_rmse_and_mae_equal_numpy_on_the_reported_residuals(
    two_orgs, ml_env, django_capture_on_commit_callbacks
):
    import numpy as np

    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    dataset_id = ml.ingest_frame(owner, a.slug, ml.churn_rows(ROWS), capture, name="Spend", filename="spend.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="monthly_spend_inr",
                                task_type="regression")
    project_id = str(ml.pick(project, "id"))
    exp = ml.train(owner, a.slug, project_id, capture, algorithm=ml.recommended(owner, a.slug, project_id)[0])
    reported = ml.holdout_metrics(exp)
    rmse = ml.metric(reported, "rmse", "RMSE")
    mae = ml.metric(reported, "mae", "MAE")
    assert rmse >= mae - 1e-9, "RMSE can never be below MAE"
    if "r2" in reported:
        assert float(reported["r2"]) <= 1.0 + 1e-9
    residuals = _residuals(exp)
    if residuals is None:
        pytest.skip("the experiment exposes no per-row test residuals; RMSE/MAE oracle cannot run (requested)")
    r = np.asarray(residuals, dtype=float)
    assert 0.1 * ROWS <= len(r) <= 0.4 * ROWS, f"{len(r)} residuals: not the held-out split"
    assert math.isclose(rmse, float(np.sqrt(np.mean(r**2))), rel_tol=1e-6, abs_tol=1e-6)
    assert math.isclose(mae, float(np.mean(np.abs(r))), rel_tol=1e-6, abs_tol=1e-6)


# ------------------------------------------------------------------------------------------------ determinism
def _numeric_leaves(value, prefix="") -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            if any(t in str(k).lower() for t in ("duration", "seconds", "time", "_at", "latency")):
                continue
            out.update(_numeric_leaves(v, f"{prefix}{k}."))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(_numeric_leaves(v, f"{prefix}{i}."))
    elif isinstance(value, int | float) and not isinstance(value, bool):
        out[prefix.rstrip(".")] = float(value)
    return out


def test_same_random_state_gives_identical_metrics(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    dataset_id = ml.ingest_frame(owner, a.slug, ml.churn_rows(ROWS), capture, name="Churn", filename="churn.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned")
    project_id = str(ml.pick(project, "id"))
    for algorithm in ml.recommended(owner, a.slug, project_id)[:2]:
        first = ml.train(owner, a.slug, project_id, capture, algorithm=algorithm, random_state=11)
        second = ml.train(owner, a.slug, project_id, capture, algorithm=algorithm, random_state=11)
        for exp in (first, second):
            assert int(ml.pick(exp, "random_state", "hyperparameters.random_state", "params.random_state")) == 11
        m1, m2 = _numeric_leaves(ml.pick(first, "metrics")), _numeric_leaves(ml.pick(second, "metrics"))
        assert m1 and m1.keys() == m2.keys(), (algorithm, sorted(m1), sorted(m2))
        diff = {k: (m1[k], m2[k]) for k in m1 if not math.isclose(m1[k], m2[k], rel_tol=1e-9, abs_tol=1e-12)}
        assert diff == {}, f"{algorithm}: same seed, different metrics: {diff}"


# ------------------------------------------------------------------------------------------------ Gemini
def test_gemini_never_receives_row_values_on_the_free_tier(two_orgs, ml_env, django_capture_on_commit_callbacks):
    from nexora.contracts import PayloadClass

    fake = ml_env
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    rows = ml.churn_rows(ROWS)
    canaries = [f"QaCanaryRegion{i}" for i in range(4)]
    for i, r in enumerate(rows):
        r["region"] = canaries[i % 4]  # frequent values: they WOULD show up as "top categorical values"
        r["customer_id"] = f"QA-CANARY-CUST-{i:05d}"
    dataset_id = ml.ingest_frame(owner, a.slug, rows, capture, name="Churn canary", filename="churn_canary.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned")
    project_id = str(ml.pick(project, "id"))
    with capture(execute=True):
        eda = owner.post(f"/api/v1/orgs/{a.slug}/ml/projects/{project_id}/eda", {"explain": True}, format="json")
    assert eda.status_code in (200, 202), eda.content[:300]
    ml.train(owner, a.slug, project_id, capture, algorithm=ml.recommended(owner, a.slug, project_id)[0])

    for call in [*fake.calls, *fake.blocked]:
        items = getattr(call.payload, "items", None) or {}
        assert PayloadClass.VALUES not in items, f"{call.prompt_key}: ML declared row VALUES on the free tier"
        sent = call.text + json.dumps(items, default=str)
        for needle in (*canaries, "QA-CANARY-CUST-"):
            assert needle not in sent, f"row value {needle!r} reached Gemini via {call.prompt_key!r}"


# ------------------------------------------------------------------------------------------------ predict_frame
def test_predict_frame_contract(two_orgs, ml_env, django_capture_on_commit_callbacks):
    import polars as pl

    services = pytest.importorskip("apps.ml.services")
    if not hasattr(services, "predict_frame") or not hasattr(services, "ModelNotReady"):
        pytest.fail("CONTRACT: apps.ml.services.predict_frame and ModelNotReady must exist")
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    rows = ml.churn_rows(ROWS)
    dataset_id = ml.ingest_frame(owner, a.slug, rows, capture, name="Churn", filename="churn.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned",
                                task_type="classification")
    project_id = str(ml.pick(project, "id"))
    exp = ml.train(owner, a.slug, project_id, capture, algorithm=ml.recommended(owner, a.slug, project_id)[0])
    model_id, version = ml.register(owner, a.slug, str(ml.pick(exp, "id")))
    frame = pl.DataFrame(ml.feature_rows(rows[:20], ml.project_features(project)))
    with a.tenant(), pytest.raises(services.ModelNotReady):
        services.predict_frame(model_id, frame)  # registered, but neither staging nor production
    assert ml.promote(owner, a.slug, model_id, version, "production").status_code == 200
    with a.tenant():
        out = services.predict_frame(model_id, frame, explain=True)
    assert out.height == frame.height
    assert set(frame.columns) <= set(out.columns)
    assert {"prediction", "probability", "explanation"} <= set(out.columns), out.columns
    probs = out["probability"].to_list()
    assert all(0.0 <= float(p) <= 1.0 for p in probs)
    api = ml.predict(owner, a.slug, model_id, frame.to_dicts())
    assert api.status_code == 200
    api_values = [ml.pick(p, "value", "prediction") if isinstance(p, dict) else p for p in ml.predictions(api)]
    assert [str(v) for v in api_values] == [str(v) for v in out["prediction"].to_list()], "API and service disagree"


def test_only_one_production_version_per_model(two_orgs, ml_env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner, analyst = api_client_for(a.owner), api_client_for(a.analyst)
    capture = django_capture_on_commit_callbacks
    dataset_id = ml.ingest_frame(owner, a.slug, ml.churn_rows(ROWS), capture, name="Churn", filename="churn.csv")
    project = ml.create_project(owner, a.slug, dataset_id=dataset_id, target="churned")
    project_id = str(ml.pick(project, "id"))
    algos = ml.recommended(owner, a.slug, project_id)
    e1 = ml.train(owner, a.slug, project_id, capture, algorithm=algos[0])
    model_id, v1 = ml.register(owner, a.slug, str(ml.pick(e1, "id")))
    e2 = ml.train(owner, a.slug, project_id, capture, algorithm=algos[min(1, len(algos) - 1)], random_state=5)
    model_id2, v2 = ml.register(owner, a.slug, str(ml.pick(e2, "id")), model_id=model_id)
    if model_id2 != model_id:
        pytest.skip("registering into an existing model is not supported; one version per model")
    # the analyst lacks ml.promote (CONTRACT: Admin/Owner only)
    assert ml.promote(analyst, a.slug, model_id, v1).status_code == 403
    assert ml.promote(owner, a.slug, model_id, v1).status_code == 200
    assert ml.promote(owner, a.slug, model_id, v2).status_code == 200
    ModelVersion = ml.ml_model("ModelVersion")
    first, second = ml.model_version(a, model_id, v1), ml.model_version(a, model_id, v2)
    with a.tenant():
        stages = {str(v.id): v.stage for v in ModelVersion.objects.filter(id__in=[first.id, second.id])}
    assert stages == {str(first.id): "archived", str(second.id): "production"}, stages
