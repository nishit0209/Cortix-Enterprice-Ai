"""Explanations (CONTRACT "Safety rules for ML": local SHAP, bounded to the first 1,000 rows).

* Tree models (random forest, XGBoost, histogram boosting): `shap.TreeExplainer`.
* Linear models: `shap.LinearExplainer` with an independent masker built from the training MEANS of the encoded
  features (stored in the artifact as aggregates, never training rows).
* Anything else, or when SHAP fails: ablation (contribution = f(x) - f(x with that column at its training
  median / mode)), a model-agnostic local attribution.

Contributions are computed on the encoded matrix and summed back to the ORIGINAL columns (one transformer per
column, see preprocess.column_slices), so a one-hot categorical gets one contribution. Units: log-odds for XGBoost
classifiers, probability for other classifiers' positive / predicted class, target units for regression.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .algorithms import LINEAR, TREE
from .preprocess import aggregate_columns, column_slices, transform

MAX_EXPLAIN_ROWS = 1000
SUMMARY_ROWS = 200
TOP_K = 5


def _pick_class(values: np.ndarray, class_index: np.ndarray | int | None) -> np.ndarray:
    """SHAP output (n, f) or (n, f, C) / list of C arrays -> (n, f) for the chosen class per row."""
    if isinstance(values, list):
        values = np.stack(values, axis=-1)
    values = np.asarray(values, dtype=float)
    if values.ndim == 2:
        return values
    n = values.shape[0]
    if class_index is None:
        return np.abs(values).mean(axis=-1)
    idx = np.broadcast_to(np.asarray(class_index), (n,))
    return values[np.arange(n), :, idx]


def _shap_values(bundle, Xt: np.ndarray, class_index) -> tuple[np.ndarray, str] | None:
    import shap

    model = bundle.pipeline.named_steps["model"]
    algo = bundle.algorithm
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            if algo in TREE:
                explainer = shap.TreeExplainer(model)
                vals = explainer.shap_values(Xt, check_additivity=False)
                return _pick_class(vals, class_index), "shap_tree"
            if algo in LINEAR and bundle.encoded_means is not None:
                means = np.asarray(bundle.encoded_means, dtype=float).reshape(1, -1)
                explainer = shap.LinearExplainer(model, shap.maskers.Independent(means, max_samples=1))
                vals = explainer.shap_values(Xt)
                return _pick_class(vals, class_index), "shap_linear"
        except Exception:  # noqa: BLE001 - fall back to ablation
            return None
    return None


def _score(bundle, X: pd.DataFrame, class_index) -> np.ndarray:
    pipe = bundle.pipeline
    if bundle.task == "classification" and hasattr(pipe, "predict_proba"):
        proba = pipe.predict_proba(X)
        if class_index is None:
            return proba.max(axis=1)
        idx = np.broadcast_to(np.asarray(class_index), (len(X),))
        return proba[np.arange(len(X)), idx]
    return np.asarray(pipe.predict(X), dtype=float)


def _ablation(bundle, X: pd.DataFrame, class_index) -> dict[str, np.ndarray]:
    base = _score(bundle, X, class_index)
    out = {}
    for col in bundle.feature_schema:
        name = col["name"]
        shifted = X.copy()
        shifted[name] = col.get("baseline")
        out[name] = base - _score(bundle, shifted, class_index)
    return out


def contributions(bundle, X: pd.DataFrame, *, class_index=None) -> tuple[dict[str, np.ndarray], str]:
    """{original column: per-row contribution array}, method."""
    Xt = transform(bundle.pipeline, X)
    got = _shap_values(bundle, Xt, class_index)
    if got is not None:
        vals, method = got
        return aggregate_columns(vals, column_slices(bundle.pipeline)), method
    return _ablation(bundle, X, class_index), "ablation"


def explain_rows(bundle, X: pd.DataFrame, *, class_index=None, top: int = TOP_K) -> tuple[list[list[dict]], str]:
    """Top-`top` contributions per row (first MAX_EXPLAIN_ROWS rows; later rows get [])."""
    n = len(X)
    head = X.iloc[:MAX_EXPLAIN_ROWS]
    if class_index is not None and not np.isscalar(class_index):
        class_index = np.asarray(class_index)[:MAX_EXPLAIN_ROWS]
    contrib, method = contributions(bundle, head, class_index=class_index)
    names = list(contrib)
    matrix = np.column_stack([contrib[c] for c in names]) if names else np.zeros((len(head), 0))
    rows: list[list[dict]] = []
    for i in range(len(head)):
        order = np.argsort(-np.abs(matrix[i]))[:top]
        rows.append([{"feature": names[j], "contribution": round(float(matrix[i, j]), 6),
                      "value": _display(head.iloc[i][names[j]])} for j in order])
    rows.extend([] for _ in range(n - len(head)))
    return rows, method


def _display(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v if isinstance(v, int | float | str | bool) else str(v)


def shap_summary(bundle, X: pd.DataFrame, *, random_state: int = 0) -> dict:
    """Global mean |contribution| per original column on a bounded sample."""
    sample = X.sample(min(len(X), SUMMARY_ROWS), random_state=random_state) if len(X) > SUMMARY_ROWS else X
    class_index = None
    if bundle.task == "classification" and len(bundle.classes or []) == 2:
        class_index = 1
    contrib, method = contributions(bundle, sample, class_index=class_index)
    items = sorted(({"feature": k, "mean_abs": float(np.mean(np.abs(v)))} for k, v in contrib.items()),
                   key=lambda d: -d["mean_abs"])
    return {"method": method, "rows": int(len(sample)), "features": items}


def native_importance(bundle) -> list[dict] | None:
    """Model-native importances (trees: impurity/gain; linear: |coef| x encoded std) per original column."""
    model = bundle.pipeline.named_steps["model"]
    slices = column_slices(bundle.pipeline)
    raw = None
    if hasattr(model, "feature_importances_"):
        raw = np.asarray(model.feature_importances_, dtype=float)
    elif hasattr(model, "coef_"):
        coef = np.asarray(model.coef_, dtype=float)
        raw = np.abs(coef).mean(axis=0) if coef.ndim == 2 else np.abs(coef)
        if bundle.encoded_std is not None:
            raw = raw * np.asarray(bundle.encoded_std, dtype=float)
    if raw is None:
        return None
    agg = aggregate_columns(raw, slices)
    total = sum(float(v) for v in agg.values()) or 1.0
    return sorted(({"feature": k, "importance": float(v) / total} for k, v in agg.items()),
                  key=lambda d: -d["importance"])


def permutation_importance(pipeline, X: pd.DataFrame, y: pd.Series, *, scoring: str | None, random_state: int,
                           columns: Sequence[str], n_repeats: int = 5, max_rows: int = 2000) -> list[dict]:
    """Permutation importance of the ORIGINAL columns through the full pipeline. `y` is aligned with `X.index`."""
    from sklearn.inspection import permutation_importance as sk_perm

    if len(X) > max_rows:
        X = X.sample(max_rows, random_state=random_state)
        y = y.loc[X.index]
    result = sk_perm(pipeline, X[list(columns)], y.to_numpy(), scoring=scoring, n_repeats=n_repeats,
                     random_state=random_state, n_jobs=1)
    items = [{"feature": c, "importance": float(m), "std": float(s)}
             for c, m, s in zip(columns, result.importances_mean, result.importances_std, strict=True)]
    return sorted(items, key=lambda d: -d["importance"])


def top_drivers(contrib: Mapping[str, float], k: int = TOP_K) -> list[dict]:
    return [{"feature": f, "contribution": float(v)} for f, v in
            sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:k]]
