"""Real CPU training on small synthetic frames (deterministic seeds) and on the testdata CSVs. No database."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from apps.ml.engine.bundle import dumps, loads_verified, sha256
from apps.ml.engine.explain import explain_rows
from apps.ml.engine.forecast import infer_freq, prepare_series, season_length, train_forecast
from apps.ml.engine.frames import infer_kind
from apps.ml.engine.leakage import leakage_guard
from apps.ml.engine.training import TrainingError, TrainSpec, cap_rows, select_k, train

TESTDATA = Path(__file__).resolve().parents[4] / "testdata" / "generated"


def _classification_frame(n=600, seed=7) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    a = rng.normal(size=n)
    b = rng.normal(size=n)
    plan = rng.choice(["basic", "pro"], size=n)
    logit = 2.0 * a - 1.5 * b + np.where(plan == "basic", 0.8, -0.8)
    y = (rng.uniform(size=n) < 1 / (1 + np.exp(-logit))).astype(int)
    return pd.DataFrame({"a": a, "b": b, "plan": plan, "noise": rng.normal(size=n), "y": y})


def _regression_frame(n=600, seed=3) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    a = rng.uniform(0, 10, size=n)
    cat = rng.choice(["x", "y", "z"], size=n)
    y = 3.0 * a + np.select([cat == "x", cat == "y"], [5.0, -5.0], 0.0) + rng.normal(0, 1.0, size=n)
    return pd.DataFrame({"a": a, "cat": cat, "y": y})


KINDS_C = {"a": "numeric", "b": "numeric", "plan": "categorical", "noise": "numeric"}


@pytest.mark.parametrize("algo", ["logistic_regression", "random_forest", "xgboost", "hist_gradient_boosting"])
def test_classification_algorithms_learn_signal(algo):
    df = _classification_frame()
    spec = TrainSpec(task="classification", algorithm=algo, features=list(KINDS_C), kinds=KINDS_C, target="y",
                     params={"n_estimators": 60} if algo in ("random_forest", "xgboost") else {},
                     random_state=42, cv_folds=3)
    res = train(df, spec)
    test = res.metrics["test"]
    assert res.metrics["primary_metric"] == "roc_auc"
    assert test["roc_auc"] > 0.8, test
    assert 0.6 < test["accuracy"] <= 1.0
    for key in ("precision_macro", "recall_macro", "f1_macro", "f1_weighted", "pr_auc", "log_loss"):
        assert test[key] is not None
    assert res.metrics["cv"]["mean"] is not None and res.metrics["cv"]["std"] is not None
    assert res.confusion_matrix["labels"] == ["0", "1"]
    assert sum(map(sum, res.confusion_matrix["matrix"])) == res.test_rows
    assert res.shap_summary and res.shap_summary["features"]
    top = {f["feature"] for f in res.shap_summary["features"][:2]}
    assert top & {"a", "b"}, res.shap_summary  # the real drivers dominate
    assert res.reference_stats["features"]["a"]["type"] == "numeric"
    # the artifact round-trips through sha256 verification and predicts with the full pipeline
    data = dumps(res.bundle)
    bundle = loads_verified(data, sha256(data))
    out = bundle.predict(df[list(KINDS_C)].head(5))
    assert out["proba"].shape == (5, 2) and set(out["prediction"]) <= {0, 1}
    drivers, method = explain_rows(bundle, df[list(KINDS_C)].head(3), class_index=out["class_index"][:3])
    assert len(drivers) == 3 and drivers[0] and method in ("shap_tree", "shap_linear", "ablation")


def test_classification_is_deterministic_for_a_seed():
    df = _classification_frame()
    spec = TrainSpec(task="classification", algorithm="random_forest", features=list(KINDS_C), kinds=KINDS_C,
                     target="y", params={"n_estimators": 40}, random_state=11, cv_folds=3)
    first, second = train(df, spec).metrics["test"], train(df, spec).metrics["test"]
    assert first.keys() == second.keys()
    for key, value in first.items():  # parallel tree sums may differ in the last ulp
        assert second[key] == (pytest.approx(value, rel=1e-9) if isinstance(value, float) else value)


@pytest.mark.parametrize("algo", ["ridge", "linear_regression", "random_forest", "xgboost", "hist_gradient_boosting"])
def test_regression_algorithms_fit_known_relationship(algo):
    df = _regression_frame()
    kinds = {"a": "numeric", "cat": "categorical"}
    spec = TrainSpec(task="regression", algorithm=algo, features=["a", "cat"], kinds=kinds, target="y",
                     params={"n_estimators": 80} if algo in ("random_forest", "xgboost") else {},
                     random_state=42, cv_folds=3)
    res = train(df, spec)
    test = res.metrics["test"]
    assert res.metrics["primary_metric"] == "rmse"
    assert test["r2"] > 0.9, test
    assert test["rmse"] < 2.5 and test["mae"] < 2.0
    assert test["mape"] is not None
    assert res.residuals["points"]


def test_multiclass_and_imbalance():
    rng = np.random.RandomState(0)
    n = 900
    a = rng.normal(size=n)
    y = np.where(a < -0.5, "low", np.where(a > 0.5, "high", "mid"))
    df = pd.DataFrame({"a": a, "b": rng.normal(size=n), "y": y})
    res = train(df, TrainSpec(task="classification", algorithm="logistic_regression", features=["a", "b"],
                              kinds={"a": "numeric", "b": "numeric"}, target="y", random_state=1, cv_folds=3))
    assert res.metrics["primary_metric"] == "f1_macro"
    assert res.metrics["test"]["f1_macro"] > 0.8
    assert res.bundle.classes == ["high", "low", "mid"]


def test_training_errors_are_explicit():
    df = _classification_frame(n=6)
    with pytest.raises(TrainingError):
        train(df, TrainSpec(task="classification", algorithm="xgboost", features=list(KINDS_C), kinds=KINDS_C,
                            target="y"))
    with pytest.raises(TrainingError):
        train(_classification_frame(), TrainSpec(task="classification", algorithm="xgboost", features=["a"],
                                                 kinds=KINDS_C, target="missing"))


def test_cap_rows_is_stratified_and_recorded():
    df = _classification_frame(n=1000)
    out, info = cap_rows(df, 200, stratify=df["y"], random_state=1)
    assert info["sampled"] and info["method"] == "stratified" and abs(len(out) - 200) <= 2
    assert abs(out["y"].mean() - df["y"].mean()) < 0.02


def test_max_rows_cap_applies_in_training():
    df = _classification_frame(n=1000)
    res = train(df, TrainSpec(task="classification", algorithm="logistic_regression", features=list(KINDS_C),
                              kinds=KINDS_C, target="y", max_rows=300, cv_folds=3))
    assert res.sample_info["sampled"] and res.train_rows + res.test_rows <= 302


# ------------------------------------------------------------------------------------------------ clustering
def test_kmeans_selects_k_by_silhouette():
    rng = np.random.RandomState(0)
    centers = np.array([[0, 0], [8, 8], [0, 8], [8, 0]])
    pts = np.vstack([c + rng.normal(0, 0.6, size=(80, 2)) for c in centers])
    k, sweep = select_k(pts, k_min=2, k_max=8, random_state=0)[:2]
    assert k == 4
    df = pd.DataFrame(pts, columns=["p", "q"])
    res = train(df, TrainSpec(task="clustering", algorithm="kmeans", features=["p", "q"],
                              kinds={"p": "numeric", "q": "numeric"}, random_state=0))
    assert res.params["n_clusters"] == 4
    assert res.metrics["primary_metric"] == "silhouette" and res.metrics["primary_value"] > 0.6
    assert sorted(res.metrics["test"]["sizes"]) == [80, 80, 80, 80]


# ------------------------------------------------------------------------------------------------ forecast
def _seasonal_series(n=140) -> pd.DataFrame:
    days = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.RandomState(0)
    weekly = np.array([10, 12, 14, 16, 30, 40, 20], dtype=float)
    y = 100 + 0.3 * np.arange(n) + weekly[np.arange(n) % 7] + rng.normal(0, 1.0, n)
    return pd.DataFrame({"day": days.strftime("%Y-%m-%d"), "sales": y})


def test_forecast_detects_season_and_beats_naive_on_trend():
    df = _seasonal_series()
    series, info = prepare_series(df, "day", "sales")
    assert info["frequency"] == "D" and infer_freq(pd.to_datetime(df["day"])) == "D"
    assert season_length("D", len(series)) == 7
    out = train_forecast(series, info, algorithm="ets")
    m = out["metrics"]
    assert m["season_length"] == 7 and m["horizon"] >= 1
    assert m["mae"] < m["baseline_mae"], m  # trend + season beats repeating last week
    assert m["mae_vs_baseline"] < 1.0
    fut = out["model"].forecast(7)
    assert len(fut) == 7 and (fut["lower"] <= fut["mean"]).all() and (fut["mean"] <= fut["upper"]).all()
    at = out["model"].forecast_at(["2025-05-25"])
    assert len(at) == 1


@pytest.mark.parametrize("algo", ["seasonal_naive", "ets", "sarimax"])
def test_forecast_algorithms_through_train(algo):
    df = _seasonal_series(98)
    res = train(df, TrainSpec(task="forecast", algorithm=algo, features=[], kinds={"day": "datetime"},
                              target="sales", time_column="day", random_state=0))
    assert res.metrics["primary_metric"] == "mae"
    assert res.metrics["test"]["mae"] is not None and res.metrics["test"]["baseline_mae"] is not None
    assert res.curves["future"] and res.bundle.forecast is not None


# --------------------------------------------------------------------------------- end to end on testdata CSVs
@pytest.mark.skipif(not (TESTDATA / "churn.csv").exists(), reason="testdata not generated")
def test_e2e_churn_classification_on_testdata():
    df = pd.read_csv(TESTDATA / "churn.csv")
    kinds = {c: infer_kind(c, df[c]) for c in df.columns}
    guard = leakage_guard(df, "churned", [c for c in df.columns if c != "churned"], kinds)
    assert "customer_id" in {d["column"] for d in guard["dropped"]}
    res = train(df, TrainSpec(task="classification", algorithm="logistic_regression", features=guard["features"],
                              kinds=kinds, target="churned", random_state=42))
    auc = res.metrics["test"]["roc_auc"]
    print(f"churn logistic_regression test ROC-AUC={auc:.4f} acc={res.metrics['test']['accuracy']:.4f}")
    # oracle AUC (true probabilities) is 0.8132: a good model approaches it without exceeding it by much
    assert 0.72 < auc < 0.87


@pytest.mark.skipif(not (TESTDATA / "sales.csv").exists(), reason="testdata not generated")
def test_e2e_revenue_regression_on_testdata():
    df = pd.read_csv(TESTDATA / "sales.csv")
    kinds = {c: infer_kind(c, df[c]) for c in df.columns}
    guard = leakage_guard(df, "revenue", ["region", "category", "quantity", "unit_price", "discount", "channel"],
                          kinds)
    res = train(df, TrainSpec(task="regression", algorithm="xgboost", features=guard["features"], kinds=kinds,
                              target="revenue", params={"n_estimators": 200}, random_state=42))
    test = res.metrics["test"]
    print(f"revenue xgboost test R2={test['r2']:.4f} RMSE={test['rmse']:.2f} MAE={test['mae']:.2f}")
    # revenue = quantity * unit_price * (1 - discount) with a heavy tail (max ~135k vs median ~1.3k): trees cannot
    # extrapolate the few extreme orders in the test split, so R2 is ~0.81 while MAE stays far below the median.
    assert test["r2"] > 0.75
    assert test["mae"] < df["revenue"].median() * 0.25
