"""Training and evaluation (ML_ARCHITECTURE §6), real and on CPU.

Supervised (classification / regression):
  * rows with a missing target are dropped; above `max_rows` a (stratified) sample is used and recorded;
  * split: stratified train/test (time-ordered when the project has a time column), test_size 0.2;
  * optional bounded randomized search (<= 20 candidates, 3-fold, inside the time budget);
  * final fit on the train split, k-fold CV on the train split (stratified / TimeSeriesSplit), budget-aware;
  * test metrics, train metrics (overfitting check), native + permutation importance, SHAP summary,
    drift reference stats.
Forecast: holdout of the last periods vs a seasonal-naive baseline, refit on the full history.
Clustering: K-Means with a silhouette sweep over k, cluster sizes and profiles.

The time budget (`time_budget_s`, ML_MAX_TRAIN_SECONDS) is checked between steps: optional steps (search, CV
folds, permutation importance, SHAP) are reduced or skipped with a warning when the budget would be exceeded.
A single estimator fit cannot be interrupted; the budget is enforced before starting each step.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import drift, explain
from .algorithms import LINEAR, SCALE_SENSITIVE, SEARCH_SPACES, default_params, make_estimator, validate_params
from .bundle import ModelBundle, lib_versions
from .forecast import prepare_series, train_forecast
from .frames import coerce_series, feature_schema, jsonable
from .metrics import classification_metrics, regression_metrics, scorer_name
from .preprocess import DEFAULT_TOP_K, build_pipeline, column_slices, transform
from .recommend import primary_metric_for

Progress = Callable[[float, str], None]
MIN_ROWS = 10
MAX_SEARCH_CANDIDATES = 20


class TrainingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TrainSpec:
    task: str
    algorithm: str
    features: Sequence[str]
    kinds: dict[str, str]
    target: str | None = None
    params: dict = field(default_factory=dict)
    random_state: int = 42
    time_column: str | None = None
    test_size: float = 0.2
    cv_folds: int = 5
    time_budget_s: float = 900.0
    tune: bool = False
    primary_metric: str | None = None
    horizon: int | None = None
    aggregation: str = "sum"
    top_k: int = DEFAULT_TOP_K
    max_rows: int | None = None


@dataclass
class TrainResult:
    bundle: ModelBundle
    metrics: dict
    params: dict
    confusion_matrix: dict | None = None
    residuals: dict | None = None
    curves: dict = field(default_factory=dict)
    feature_importances: dict = field(default_factory=dict)
    shap_summary: dict | None = None
    reference_stats: dict = field(default_factory=dict)
    sample_info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    train_rows: int = 0
    test_rows: int = 0
    duration_s: float = 0.0


class _Budget:
    def __init__(self, seconds: float):
        self.start = time.monotonic()
        self.seconds = max(float(seconds), 1.0)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start

    @property
    def remaining(self) -> float:
        return self.seconds - self.elapsed


def cap_rows(df: pd.DataFrame, max_rows: int | None, *, stratify: pd.Series | None = None,
             random_state: int = 42) -> tuple[pd.DataFrame, dict]:
    """Sample down to `max_rows` (stratified by `stratify` when given). Returns (frame, sample_info)."""
    n = len(df)
    if not max_rows or n <= max_rows:
        return df, {"sampled": False, "source_rows": n, "rows": n}
    frac = max_rows / n
    if stratify is not None:
        keys = stratify.astype(str).fillna("__missing__")
        parts = [g.sample(max(1, int(round(len(g) * frac))), random_state=random_state)
                 for _, g in df.groupby(keys, sort=True)]
        out = pd.concat(parts).sort_index()
        method = "stratified"
    else:
        out = df.sample(max_rows, random_state=random_state).sort_index()
        method = "random"
    return out, {"sampled": True, "source_rows": n, "rows": int(len(out)), "method": method,
                 "max_rows": int(max_rows)}


def _noop(_pct: float, _msg: str) -> None:
    return None


def train(df: pd.DataFrame, spec: TrainSpec, progress: Progress | None = None) -> TrainResult:
    progress = progress or _noop
    budget = _Budget(spec.time_budget_s)
    if spec.task == "forecast":
        result = _train_forecast(df, spec, progress)
    elif spec.task == "clustering":
        result = _train_clustering(df, spec, progress, budget)
    elif spec.task in ("classification", "regression"):
        result = _train_supervised(df, spec, progress, budget)
    else:
        raise TrainingError("invalid_task", f"Unknown task {spec.task!r}.")
    result.duration_s = round(budget.elapsed, 3)
    result.bundle.versions = lib_versions()
    return result


# ------------------------------------------------------------------------------------------- supervised
def _labels(y: pd.Series) -> tuple[np.ndarray, list]:
    """Encode class labels as 0..K-1. Integral numeric labels stay ints, everything else becomes strings."""
    if pd.api.types.is_numeric_dtype(y.dtype) and not pd.api.types.is_bool_dtype(y.dtype):
        values = y.astype(float)
        if np.all(np.mod(values, 1) == 0):
            raw = values.astype(int).to_numpy()
        else:
            raw = values.astype(str).to_numpy()
    else:
        raw = y.map(str).to_numpy()
    classes, codes = np.unique(raw, return_inverse=True)
    return codes.astype(int), [c.item() if isinstance(c, np.generic) else c for c in classes]


def _split(X: pd.DataFrame, y: pd.Series, spec: TrainSpec, order: pd.Series | None, stratify: bool):
    from sklearn.model_selection import train_test_split

    n = len(X)
    n_test = max(1, int(round(n * spec.test_size)))
    if order is not None:
        idx = order.sort_values(kind="mergesort").index
        train_idx, test_idx = idx[: n - n_test], idx[n - n_test:]
        return X.loc[train_idx], X.loc[test_idx], y.loc[train_idx], y.loc[test_idx], "time_ordered"
    strat = y if stratify else None
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=n_test, random_state=spec.random_state, stratify=strat)
    return Xtr, Xte, ytr, yte, "stratified" if stratify else "random"


def _cv_splitter(spec: TrainSpec, folds: int, classification: bool, time_ordered: bool):
    from sklearn.model_selection import KFold, StratifiedKFold, TimeSeriesSplit

    if time_ordered:
        return TimeSeriesSplit(n_splits=folds)
    if classification:
        return StratifiedKFold(n_splits=folds, shuffle=True, random_state=spec.random_state)
    return KFold(n_splits=folds, shuffle=True, random_state=spec.random_state)


def _cv_scoring(metric: str, n_classes: int) -> str | None:
    if metric == "roc_auc" and n_classes > 2:
        return "roc_auc_ovr"
    if metric == "pr_auc" and n_classes > 2:
        return "f1_macro"
    return scorer_name(metric)


def _train_supervised(df: pd.DataFrame, spec: TrainSpec, progress: Progress, budget: _Budget) -> TrainResult:
    from sklearn.base import clone
    from sklearn.model_selection import RandomizedSearchCV, cross_validate

    classification = spec.task == "classification"
    target = spec.target
    if not target or target not in df.columns:
        raise TrainingError("target_missing", "The target column is not in the training data.")
    features = [f for f in spec.features if f in df.columns]
    if not features:
        raise TrainingError("no_features", "No usable feature columns remain.")
    warnings_out: list[str] = []
    cols = list(dict.fromkeys([*features, target, *([spec.time_column] if spec.time_column else [])]))
    data = df[cols]
    data = data[data[target].notna()]
    if not classification:
        data = data[coerce_series(data[target], "numeric").notna()]
    if len(data) < MIN_ROWS:
        raise TrainingError("too_few_rows", f"At least {MIN_ROWS} rows with a target value are needed.")
    data, sample_info = cap_rows(data, spec.max_rows, stratify=data[target] if classification else None,
                                 random_state=spec.random_state)
    if sample_info["sampled"]:
        warnings_out.append(f"Trained on a {sample_info['method']} sample of {sample_info['rows']:,} of "
                            f"{sample_info['source_rows']:,} rows (ML_MAX_TRAIN_ROWS).")
    progress(10, "Preparing features")
    schema = feature_schema(data, features, spec.kinds, top_k=spec.top_k)
    X = data[features]
    if classification:
        codes, classes = _labels(data[target])
        y = pd.Series(codes, index=data.index)
        n_classes = len(classes)
        if n_classes < 2:
            raise TrainingError("single_class", "The target has a single class; nothing to learn.")
        counts = np.bincount(codes, minlength=n_classes)
        minority = float(counts.min() / counts.sum())
        stratify = bool(counts.min() >= 2)
    else:
        classes, n_classes, minority, stratify = None, 0, None, False
        y = coerce_series(data[target], "numeric")
    metric = spec.primary_metric or primary_metric_for(spec.task, n_classes=n_classes, minority_share=minority)
    order = coerce_series(data[spec.time_column], "datetime") if spec.time_column else None
    Xtr, Xte, ytr, yte, split_kind = _split(X, y, spec, order, stratify)
    if classification and len(np.unique(ytr)) < 2:
        raise TrainingError("single_class", "The training split has a single class.")

    params = {**default_params(spec.algorithm, n_rows=len(Xtr)), **validate_params(spec.algorithm, spec.params)}
    balanced = bool(classification and minority is not None and minority < 0.10)
    pos_weight = float(counts[0] / counts[1]) if classification and n_classes == 2 and counts[1] else None
    if balanced:
        warnings_out.append("Class weights are balanced because the minority class is below 10%.")
    estimator = make_estimator(spec.task, spec.algorithm, params, random_state=spec.random_state,
                               n_classes=n_classes, balanced=balanced, pos_weight=pos_weight)
    pipeline = build_pipeline(schema, estimator, scale=spec.algorithm in SCALE_SENSITIVE, top_k=spec.top_k)
    scoring = _cv_scoring(metric, n_classes)

    progress(20, "Fitting the model")
    t_fit = time.monotonic()
    pipeline.fit(Xtr, ytr)
    fit_seconds = time.monotonic() - t_fit
    search_info = None
    if spec.tune and spec.algorithm in SEARCH_SPACES:
        space = {f"model__{k}": v for k, v in SEARCH_SPACES[spec.algorithm].items() if k not in spec.params}
        combos = math.prod(len(v) for v in space.values()) if space else 0
        affordable = int(budget.remaining * 0.5 / max(fit_seconds * 3, 1e-3))
        n_iter = min(MAX_SEARCH_CANDIDATES, combos, affordable)
        if n_iter >= 2:
            progress(30, f"Searching {n_iter} hyperparameter candidates")
            search = RandomizedSearchCV(clone(pipeline), space, n_iter=n_iter, cv=_cv_splitter(
                spec, 3, classification and stratify, split_kind == "time_ordered"), scoring=scoring,
                random_state=spec.random_state, n_jobs=1, refit=True)
            search.fit(Xtr, ytr)
            pipeline = search.best_estimator_
            best = {k.removeprefix("model__"): v for k, v in search.best_params_.items()}
            params.update(best)
            search_info = {"candidates": n_iter, "best_params": best, "best_cv_score": float(search.best_score_)}
        else:
            warnings_out.append("Hyperparameter search skipped: not enough time budget.")

    progress(45, "Cross-validating")
    folds = min(spec.cv_folds, int(np.bincount(ytr).min()) if classification and stratify else spec.cv_folds,
                max(len(Xtr) // 2, 2))
    affordable = int(budget.remaining * 0.6 / max(fit_seconds, 1e-3))
    if affordable < folds:
        warnings_out.append(f"Cross-validation reduced to {max(affordable, 0)} folds to respect the time budget.")
        folds = affordable
    cv_info: dict[str, Any] = {"metric": metric, "folds": 0}
    if folds >= 2:
        cv = cross_validate(clone(pipeline), Xtr, ytr, cv=_cv_splitter(spec, folds, classification and stratify,
                                                                        split_kind == "time_ordered"),
                            scoring=scoring, n_jobs=1, error_score=np.nan)
        scores = np.asarray(cv["test_score"], dtype=float)
        if scoring and scoring.startswith("neg_"):
            scores = -scores
        cv_info = {"metric": metric, "scoring": scoring, "folds": folds, "scores": jsonable(scores),
                   "mean": jsonable(np.nanmean(scores)) if np.isfinite(scores).any() else None,
                   "std": jsonable(np.nanstd(scores)) if np.isfinite(scores).any() else None}

    progress(65, "Evaluating on the test split")
    bundle = ModelBundle(task=spec.task, algorithm=spec.algorithm, target=target, feature_schema=schema,
                         pipeline=pipeline, classes=classes, random_state=spec.random_state,
                         time_column=spec.time_column)
    if spec.algorithm in LINEAR:
        Xt = transform(pipeline, Xtr)
        bundle.encoded_means = Xt.mean(axis=0).tolist()
        bundle.encoded_std = Xt.std(axis=0).tolist()
    confusion = residuals = None
    curves: dict = {}
    if classification:
        proba_te = pipeline.predict_proba(Xte)
        pred_te = proba_te.argmax(axis=1)
        evaluated = classification_metrics(yte.to_numpy(), pred_te, proba_te, classes)
        test_metrics, confusion, curves = evaluated["metrics"], evaluated["confusion_matrix"], evaluated["curves"]
        proba_tr = pipeline.predict_proba(Xtr)
        train_metrics = classification_metrics(ytr.to_numpy(), proba_tr.argmax(axis=1), proba_tr, classes)["metrics"]
        pred_display = np.asarray(classes, dtype=object)[pred_te]
    else:
        pred_te = pipeline.predict(Xte)
        evaluated = regression_metrics(yte.to_numpy(), pred_te, sample_seed=spec.random_state)
        test_metrics, residuals = evaluated["metrics"], evaluated["residuals"]
        train_metrics = regression_metrics(ytr.to_numpy(), pipeline.predict(Xtr))["metrics"]
        pred_display = pred_te

    progress(78, "Computing feature importance")
    importances: dict[str, Any] = {"native": explain.native_importance(bundle)}
    if budget.remaining > fit_seconds * 2:
        importances["permutation"] = explain.permutation_importance(
            pipeline, Xte, yte, scoring=scoring, random_state=spec.random_state, columns=features,
            n_repeats=5 if budget.remaining > 60 else 2)
    else:
        warnings_out.append("Permutation importance skipped to respect the time budget.")
    shap_info = None
    if budget.remaining > 0:
        progress(88, "Computing SHAP explanations")
        shap_info = explain.shap_summary(bundle, Xte, random_state=spec.random_state)
    reference = drift.reference_stats(Xtr, schema, predictions=pred_display, task=spec.task)
    metrics = {"test": test_metrics, "train": {k: train_metrics.get(k) for k in test_metrics}, "cv": cv_info,
               "primary_metric": metric, "primary_value": test_metrics.get(metric), "split": split_kind,
               "fit_seconds": round(fit_seconds, 3)}
    if classification:
        metrics["class_balance"] = {"classes": [str(c) for c in classes], "counts": counts.tolist(),
                                    "minority_share": round(minority, 4)}
    if search_info:
        metrics["search"] = jsonable(search_info)
    progress(95, "Packaging the model")
    return TrainResult(bundle=bundle, metrics=jsonable(metrics), params=jsonable(params), confusion_matrix=confusion,
                       residuals=residuals, curves=jsonable(curves), feature_importances=jsonable(importances),
                       shap_summary=jsonable(shap_info), reference_stats=jsonable(reference),
                       sample_info=sample_info, warnings=warnings_out, train_rows=len(Xtr), test_rows=len(Xte))


# ------------------------------------------------------------------------------------------- forecast
def _train_forecast(df: pd.DataFrame, spec: TrainSpec, progress: Progress) -> TrainResult:
    if not spec.time_column or not spec.target:
        raise TrainingError("forecast_needs_time", "Forecasting needs a time column and a numeric target.")
    progress(10, "Aggregating the series")
    try:
        series, info = prepare_series(df, spec.time_column, spec.target, agg=spec.aggregation)
    except ValueError as exc:
        raise TrainingError("forecast_series_invalid", str(exc)) from exc
    params = validate_params(spec.algorithm, spec.params)
    fparams: dict[str, Any] = {k: v for k, v in params.items() if k in ("trend", "damped")}
    if spec.algorithm == "sarimax" and params:
        fparams["order"] = (params.get("p", 1), params.get("d", 1), params.get("q", 1))
    progress(40, f"Fitting {spec.algorithm}")
    try:
        out = train_forecast(series, info, algorithm=spec.algorithm, params=fparams, horizon=spec.horizon)
    except (ValueError, np.linalg.LinAlgError) as exc:
        raise TrainingError("forecast_failed", f"The forecast model could not be fitted: {exc}") from exc
    bundle = ModelBundle(task="forecast", algorithm=spec.algorithm, target=spec.target,
                         feature_schema=[{"name": spec.time_column, "kind": "datetime", "required": True}],
                         forecast=out["model"], random_state=spec.random_state, time_column=spec.time_column)
    m = out["metrics"]
    metric = spec.primary_metric or "mae"
    metrics = {"test": jsonable(m), "primary_metric": metric, "primary_value": jsonable(m.get(metric)),
               "split": "time_ordered", "series": jsonable(out["info"]), "cv": {"metric": metric, "folds": 0}}
    warnings_out = []
    if m.get("mae_vs_baseline") is not None and m["mae_vs_baseline"] >= 1.0 and spec.algorithm != "seasonal_naive":
        warnings_out.append("This model does not beat the seasonal-naive baseline on the holdout periods.")
    progress(95, "Packaging the model")
    return TrainResult(bundle=bundle, metrics=metrics, params=jsonable(fparams), curves=jsonable(out["curves"]),
                       sample_info={"sampled": False, "source_rows": int(len(df)), "rows": int(len(df)),
                                    "periods": info["periods"]},
                       warnings=warnings_out, train_rows=int(m["train_periods"]), test_rows=int(m["horizon"]))


# ------------------------------------------------------------------------------------------- clustering
def select_k(Xt: np.ndarray, *, k_min: int = 2, k_max: int = 10, random_state: int = 42,
             sample: int = 5000) -> tuple[int, list[dict]]:
    """Silhouette sweep: the k with the highest silhouette (ties -> smaller k)."""
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    rng = np.random.RandomState(random_state)
    if len(Xt) > sample:
        Xt = Xt[rng.choice(len(Xt), sample, replace=False)]
    k_max = min(k_max, len(Xt) - 1)
    if k_max < k_min:
        raise TrainingError("too_few_rows", "Not enough rows to form clusters.")
    sweep = []
    for k in range(k_min, k_max + 1):
        km = KMeans(n_clusters=k, n_init=10, random_state=random_state).fit(Xt)
        if len(np.unique(km.labels_)) < 2:
            continue
        sil = float(silhouette_score(Xt, km.labels_, sample_size=min(len(Xt), 3000), random_state=random_state))
        sweep.append({"k": k, "silhouette": round(sil, 6), "inertia": float(km.inertia_)})
    if not sweep:
        raise TrainingError("no_clusters", "The data does not separate into clusters.")
    best = max(sweep, key=lambda s: (s["silhouette"], -s["k"]))
    return best["k"], sweep


def _train_clustering(df: pd.DataFrame, spec: TrainSpec, progress: Progress, budget: _Budget) -> TrainResult:
    from sklearn.cluster import KMeans
    from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score, silhouette_score

    features = [f for f in spec.features if f in df.columns]
    if not features:
        raise TrainingError("no_features", "No usable feature columns remain.")
    data, sample_info = cap_rows(df[features], spec.max_rows, random_state=spec.random_state)
    if len(data) < MIN_ROWS:
        raise TrainingError("too_few_rows", f"At least {MIN_ROWS} rows are needed.")
    params = validate_params(spec.algorithm, spec.params)
    schema = feature_schema(data, features, spec.kinds, top_k=spec.top_k)
    progress(15, "Encoding features")
    probe = build_pipeline(schema, KMeans(n_clusters=2, n_init=1, random_state=spec.random_state), scale=True,
                           top_k=spec.top_k)
    Xt = np.asarray(probe[:-1].fit_transform(data), dtype=float)
    if params.get("n_clusters"):
        k, sweep = int(params["n_clusters"]), []
    else:
        progress(30, "Choosing the number of clusters (silhouette sweep)")
        k, sweep = select_k(Xt, k_min=params.get("k_min", 2), k_max=params.get("k_max", 10),
                            random_state=spec.random_state)
    progress(60, f"Fitting K-Means with k={k}")
    pipeline = build_pipeline(schema, KMeans(n_clusters=k, n_init=10, random_state=spec.random_state), scale=True,
                              top_k=spec.top_k).fit(data)
    labels = pipeline.named_steps["model"].labels_
    Xt = transform(pipeline, data)
    sample_n = min(len(Xt), 3000)
    metrics = {
        "silhouette": float(silhouette_score(Xt, labels, sample_size=sample_n, random_state=spec.random_state)),
        "davies_bouldin": float(davies_bouldin_score(Xt, labels)),
        "calinski_harabasz": float(calinski_harabasz_score(Xt, labels)), "n_clusters": int(k),
    }
    sizes = np.bincount(labels, minlength=k)
    profiles = []
    for c in range(k):
        members = data[labels == c]
        prof: dict[str, Any] = {"cluster": c, "size": int(sizes[c]), "share": round(float(sizes[c] / len(data)), 4),
                                "numeric_means": {}, "top_categories": {}}
        for col in schema:
            values = coerce_series(members[col["name"]], col["kind"])
            if col["kind"] in ("numeric", "boolean"):
                prof["numeric_means"][col["name"]] = jsonable(values.mean())
            elif col["kind"] == "categorical" and values.notna().any():
                prof["top_categories"][col["name"]] = str(values.value_counts().index[0])
        profiles.append(prof)
    # importance: share of each column's encoded variance explained by the clusters (eta squared)
    slices = column_slices(pipeline)
    importance = []
    for name, sl in slices.items():
        block = Xt[:, sl]
        total = float(((block - block.mean(axis=0)) ** 2).sum())
        between = sum(float(((block[labels == c].mean(axis=0) - block.mean(axis=0)) ** 2).sum()) * int(sizes[c])
                      for c in range(k) if sizes[c])
        importance.append({"feature": name, "importance": between / total if total > 0 else 0.0})
    importance.sort(key=lambda d: -d["importance"])
    bundle = ModelBundle(task="clustering", algorithm="kmeans", target=None, feature_schema=schema,
                         pipeline=pipeline, random_state=spec.random_state)
    reference = drift.reference_stats(data, schema, predictions=labels, task="clustering")
    out_metrics = {"test": jsonable({**metrics, "sizes": sizes.tolist()}), "primary_metric": "silhouette",
                   "primary_value": metrics["silhouette"], "k_sweep": sweep, "split": "none",
                   "cv": {"metric": "silhouette", "folds": 0}}
    progress(95, "Packaging the model")
    return TrainResult(bundle=bundle, metrics=jsonable(out_metrics), params=jsonable({**params, "n_clusters": k}),
                       curves={"k_sweep": sweep, "profiles": jsonable(profiles)},
                       feature_importances={"native": jsonable(importance)}, reference_stats=jsonable(reference),
                       sample_info=sample_info, train_rows=len(data), test_rows=0)
