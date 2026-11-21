"""ML engine units that need no database: typing, leakage guard, preprocessing, recommendations, prediction input
validation, PSI math and artifact integrity."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from apps.ml.engine import drift
from apps.ml.engine.algorithms import InvalidHyperparameters, algorithms_for, validate_params
from apps.ml.engine.bundle import ArtifactIntegrityError, ModelBundle, dumps, loads_verified, sha256
from apps.ml.engine.eda import compute_eda, detect_task, template_narrative
from apps.ml.engine.frames import feature_schema, infer_kind, is_id_name, jsonable
from apps.ml.engine.leakage import leakage_guard
from apps.ml.engine.preprocess import build_pipeline
from apps.ml.engine.recommend import primary_metric_for, recommend
from apps.ml.engine.validation import MAX_ROWS, PredictionInputError, validate_rows


def _frame(n=200, seed=0) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    x = rng.normal(size=n)
    return pd.DataFrame({
        "customer_id": [f"C{i:05d}" for i in range(n)],
        "age": rng.randint(18, 80, size=n),
        "income": rng.normal(50_000, 10_000, size=n).round(2),
        "plan": rng.choice(["basic", "standard", "premium"], size=n),
        "signup": pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
        "x": x,
        "target": (x > 0).astype(int),
    })


# ------------------------------------------------------------------------------------------------ typing
def test_kind_inference():
    df = _frame()
    assert infer_kind("customer_id", df["customer_id"]) == "id"
    assert infer_kind("age", df["age"]) == "numeric"
    assert infer_kind("plan", df["plan"]) == "categorical"
    assert infer_kind("signup", df["signup"]) == "datetime"
    assert infer_kind("flag", pd.Series(["yes", "no"] * 20)) == "boolean"
    assert infer_kind("c", pd.Series(["same"] * 30)) == "constant"
    assert is_id_name("order_id") and is_id_name("userId") and is_id_name("id") and not is_id_name("paid")


def test_jsonable_handles_nan_numpy_and_timestamps():
    out = jsonable({"a": np.float64("nan"), "b": np.int64(3), "c": [np.inf, 1.5], "d": pd.Timestamp("2025-01-02")})
    assert out == {"a": None, "b": 3, "c": [None, 1.5], "d": "2025-01-02T00:00:00"}


# ------------------------------------------------------------------------------------------------ leakage
def test_leakage_guard_drops_ids_and_target_copies():
    df = _frame()
    df["target_copy"] = df["target"]
    df["almost_target"] = df["target"] * 10 + np.random.RandomState(1).normal(0, 0.01, len(df))
    df["row_key"] = range(len(df))  # all unique numeric
    kinds = {c: infer_kind(c, df[c]) for c in df.columns}
    cands = [c for c in df.columns if c != "target"]
    out = leakage_guard(df, "target", cands, kinds)
    dropped = {d["column"]: d["reason"] for d in out["dropped"]}
    assert dropped["customer_id"] == "id_like"
    assert dropped["target_copy"] == "identical_to_target"
    assert dropped["almost_target"] in ("near_duplicate_of_target", "identical_to_target")
    assert "row_key" in dropped
    assert {"age", "income", "plan", "x"} <= set(out["features"])
    assert "target" not in out["features"]
    assert all(d.get("message") for d in out["dropped"])


# ------------------------------------------------------------------------------------------------ EDA
def test_detect_task_and_eda_summary():
    df = _frame()
    kinds = {c: infer_kind(c, df[c]) for c in df.columns}
    assert detect_task(df, "target", kinds) == "classification"
    assert detect_task(df, "income", kinds) == "regression"
    assert detect_task(df, None, kinds) == "clustering"
    eda = compute_eda(df, target="target", task="classification", features=["age", "income", "plan", "x"],
                      kinds=kinds)
    assert eda["rows"] == 200
    assert eda["target"]["n_classes"] == 2
    assert 0 < eda["target"]["minority_share"] <= 0.5
    assert template_narrative(eda, task="classification", target="target")


# ------------------------------------------------------------------------------------------------ preprocessing
def test_pipeline_handles_unknown_categories_and_missing_values():
    from sklearn.linear_model import LogisticRegression

    df = _frame()
    kinds = {"age": "numeric", "income": "numeric", "plan": "categorical", "signup": "datetime"}
    schema = feature_schema(df, list(kinds), kinds)
    pipe = build_pipeline(schema, LogisticRegression(max_iter=500), scale=True)
    pipe.fit(df[list(kinds)], df["target"])
    unseen = pd.DataFrame([{"age": None, "income": np.nan, "plan": "never-seen", "signup": None},
                           {"age": "41", "income": "52,000", "plan": "basic", "signup": "2025-02-03"}])
    proba = pipe.predict_proba(unseen)
    assert proba.shape == (2, 2) and np.isfinite(proba).all()


# ------------------------------------------------------------------------------------------------ recommendations
def test_recommendation_rules():
    kinds = {"a": "numeric", "b": "categorical"}
    small = recommend(task="classification", n_rows=500, kinds=kinds, features=["a", "b"])
    assert small["candidates"][0]["algorithm"] == "logistic_regression"
    assert any("500 rows" in r for r in small["candidates"][0]["reasons"])
    big = recommend(task="classification", n_rows=20_000, kinds=kinds, features=["a", "b"])
    assert big["candidates"][0]["algorithm"] == "xgboost"
    assert 2 <= len(big["candidates"]) <= 4 and all(c["reasons"] for c in big["candidates"])
    imb = recommend(task="classification", n_rows=20_000, kinds=kinds, features=["a", "b"], minority_share=0.05)
    assert imb["primary_metric"] == "pr_auc" and any("Minority" in w for w in imb["warnings"])
    expl = recommend(task="regression", n_rows=20_000, kinds=kinds, features=["a", "b"],
                     business_goal="credit decisions must be explainable to the regulator")
    assert expl["explainability_required"] and expl["candidates"][0]["algorithm"] == "ridge"
    assert primary_metric_for("classification", n_classes=4) == "f1_macro"
    assert primary_metric_for("regression") == "rmse"
    fc = recommend(task="forecast", n_rows=200, kinds={}, features=[], periods=200, season_length=7)
    assert {c["algorithm"] for c in fc["candidates"]} == {"seasonal_naive", "ets", "sarimax"}


def test_hyperparameter_validation():
    assert "xgboost" in algorithms_for("classification") and "kmeans" in algorithms_for("clustering")
    validate_params("random_forest", {"n_estimators": 50, "max_depth": None})
    with pytest.raises(InvalidHyperparameters) as exc:
        validate_params("random_forest", {"n_estimators": 0, "bogus": 1})
    assert "n_estimators" in exc.value.errors and "bogus" in exc.value.errors


# ------------------------------------------------------------------------------------------------ validation
SCHEMA = [{"name": "age", "kind": "numeric"}, {"name": "plan", "kind": "categorical"},
          {"name": "signup", "kind": "datetime"}, {"name": "vip", "kind": "boolean"}]


def test_predict_validation_missing_unknown_and_types():
    ok = validate_rows([{"age": 30, "plan": "brand-new-plan", "signup": "2025-01-01", "vip": "yes"}], SCHEMA)
    assert list(ok.columns) == ["age", "plan", "signup", "vip"]
    nulls = validate_rows({"age": None, "plan": None, "signup": None, "vip": None}, SCHEMA)
    assert len(nulls) == 1
    with pytest.raises(PredictionInputError) as exc:
        validate_rows([{"age": 30}], SCHEMA)
    assert exc.value.code == "missing_columns" and exc.value.extra["missing"] == ["plan", "signup", "vip"]
    with pytest.raises(PredictionInputError) as exc:
        validate_rows([{"age": 1, "plan": "a", "signup": "2025-01-01", "vip": 1, "extra": 2}], SCHEMA)
    assert exc.value.code == "unknown_columns"
    with pytest.raises(PredictionInputError) as exc:
        validate_rows([{"age": "old", "plan": "a", "signup": "yesterday", "vip": "maybe"}], SCHEMA)
    assert exc.value.code == "invalid_values" and len(exc.value.errors) == 3
    with pytest.raises(PredictionInputError) as exc:
        validate_rows([{"age": 1, "plan": "a", "signup": "2025-01-01", "vip": 1}] * (MAX_ROWS + 1), SCHEMA)
    assert exc.value.code == "too_many_rows"
    with pytest.raises(PredictionInputError):
        validate_rows([], SCHEMA)


# ------------------------------------------------------------------------------------------------ PSI
def test_psi_matches_hand_computation():
    expected = [0.25, 0.25, 0.25, 0.25]
    actual = [0.10, 0.20, 0.30, 0.40]
    hand = sum((a - e) * math.log(a / e) for a, e in zip(actual, expected, strict=True))
    assert drift.psi(expected, actual) == pytest.approx(hand, rel=1e-9)
    assert drift.psi(expected, actual) == pytest.approx(0.228, abs=1e-3)
    assert drift.psi([10, 20, 30], [1, 2, 3]) == pytest.approx(0.0)  # counts are normalised
    assert drift.psi_status(0.05) == "ok" and drift.psi_status(0.15) == "warning" and drift.psi_status(0.3) == "alert"
    # an empty bin is floored at EPS instead of producing inf
    assert math.isfinite(drift.psi([0.5, 0.5, 0.0], [0.4, 0.4, 0.2]))


def test_reference_bins_and_drift_report():
    rng = np.random.RandomState(0)
    ref_x = pd.DataFrame({"x": rng.normal(0, 1, 2000), "c": rng.choice(["a", "b"], 2000)})
    schema = [{"name": "x", "kind": "numeric"}, {"name": "c", "kind": "categorical"}]
    ref = drift.reference_stats(ref_x, schema, predictions=rng.choice(["0", "1"], 2000), task="classification")
    same = drift.bin_counts(pd.DataFrame({"x": rng.normal(0, 1, 500), "c": rng.choice(["a", "b"], 500)}), ref,
                            predictions=rng.choice(["0", "1"], 500))
    shifted = drift.bin_counts(pd.DataFrame({"x": rng.normal(2, 1, 500), "c": ["zzz"] * 500}), ref,
                               predictions=np.array(["1"] * 500))
    assert sum(same["features"]["x"]) == 500
    ok = drift.drift_report(ref, drift.merge_counts([same]))
    bad = drift.drift_report(ref, drift.merge_counts([shifted, shifted]))
    assert ok["status"] == "ok" and ok["max_psi"] < 0.1
    assert bad["status"] == "alert" and {a["kind"] for a in bad["alerts"]} == {"feature_drift", "prediction_drift"}
    assert bad["features"]["c"]["psi"] > 0.2 and bad["rows"] == 1000
    few = drift.drift_report(ref, drift.bin_counts(ref_x.head(10), ref))
    assert few["status"] == "insufficient_data"


# ------------------------------------------------------------------------------------------------ artifacts
def test_tampered_artifact_is_refused():
    bundle = ModelBundle(task="regression", algorithm="ridge", target="y", feature_schema=[])
    data = dumps(bundle)
    digest = sha256(data)
    assert loads_verified(data, digest).algorithm == "ridge"
    tampered = data[:-1] + bytes([data[-1] ^ 0xFF])
    with pytest.raises(ArtifactIntegrityError):
        loads_verified(tampered, digest)
    with pytest.raises(ArtifactIntegrityError):
        loads_verified(data, "")
