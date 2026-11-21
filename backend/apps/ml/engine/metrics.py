"""Evaluation metrics (ML_ARCHITECTURE §6) and the primary-metric ordering used by comparisons."""

from __future__ import annotations

import math

import numpy as np

from .frames import jsonable

HIGHER_IS_BETTER = {
    "accuracy": True, "precision_macro": True, "recall_macro": True, "f1_macro": True, "f1_weighted": True,
    "precision_weighted": True, "recall_weighted": True, "roc_auc": True, "pr_auc": True, "log_loss": False,
    "mae": False, "rmse": False, "r2": True, "mape": False, "smape": False, "silhouette": True,
    "davies_bouldin": False, "calinski_harabasz": True,
}
MAX_CURVE_POINTS = 60


def _downsample(xs, ys, n=MAX_CURVE_POINTS):
    xs, ys = np.asarray(xs), np.asarray(ys)
    if len(xs) <= n:
        return xs, ys
    idx = np.unique(np.linspace(0, len(xs) - 1, n).round().astype(int))
    return xs[idx], ys[idx]


def classification_metrics(y_true: np.ndarray, y_pred: np.ndarray, proba: np.ndarray | None,
                           labels: list) -> dict:
    """y_true / y_pred are class indices 0..K-1; `labels` are the original class values (display)."""
    from sklearn import metrics as m

    k = len(labels)
    out: dict = {
        "accuracy": m.accuracy_score(y_true, y_pred),
        "precision_macro": m.precision_score(y_true, y_pred, average="macro", zero_division=0),
        "recall_macro": m.recall_score(y_true, y_pred, average="macro", zero_division=0),
        "f1_macro": m.f1_score(y_true, y_pred, average="macro", zero_division=0),
        "precision_weighted": m.precision_score(y_true, y_pred, average="weighted", zero_division=0),
        "recall_weighted": m.recall_score(y_true, y_pred, average="weighted", zero_division=0),
        "f1_weighted": m.f1_score(y_true, y_pred, average="weighted", zero_division=0),
    }
    curves: dict = {}
    if proba is not None:
        present = np.unique(y_true)
        try:
            out["log_loss"] = m.log_loss(y_true, np.clip(proba, 1e-15, 1 - 1e-15), labels=list(range(k)))
        except ValueError:
            out["log_loss"] = None
        if k == 2 and len(present) == 2:
            pos = proba[:, 1]
            out["roc_auc"] = m.roc_auc_score(y_true, pos)
            out["pr_auc"] = m.average_precision_score(y_true, pos)
            fpr, tpr, _ = m.roc_curve(y_true, pos)
            prec, rec, _ = m.precision_recall_curve(y_true, pos)
            fx, fy = _downsample(fpr, tpr)
            rx, ry = _downsample(rec[::-1], prec[::-1])
            curves["roc"] = [{"fpr": float(a), "tpr": float(b)} for a, b in zip(fx, fy, strict=True)]
            curves["pr"] = [{"recall": float(a), "precision": float(b)} for a, b in zip(rx, ry, strict=True)]
            from sklearn.calibration import calibration_curve

            try:
                frac, mean_pred = calibration_curve(y_true, pos, n_bins=10, strategy="quantile")
                curves["calibration"] = [{"predicted": float(a), "observed": float(b)}
                                         for a, b in zip(mean_pred, frac, strict=True)]
            except ValueError:
                pass
        elif k > 2 and len(present) == k:
            out["roc_auc"] = m.roc_auc_score(y_true, proba, multi_class="ovr", average="macro")
            onehot = np.eye(k)[y_true]
            out["pr_auc"] = m.average_precision_score(onehot, proba, average="macro")
        else:
            out["roc_auc"] = out["pr_auc"] = None
    cm = m.confusion_matrix(y_true, y_pred, labels=list(range(k)))
    out = jsonable(out)
    return {"metrics": out, "confusion_matrix": {"labels": [str(v) for v in labels], "matrix": cm.tolist()},
            "curves": curves}


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray, *, sample_seed: int = 0) -> dict:
    from sklearn import metrics as m

    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    resid = y_true - y_pred
    nonzero = np.abs(y_true) > 1e-9
    mape = float(np.mean(np.abs(resid[nonzero] / y_true[nonzero])) * 100) if nonzero.any() else None
    out = {"mae": m.mean_absolute_error(y_true, y_pred), "rmse": math.sqrt(m.mean_squared_error(y_true, y_pred)),
           "r2": m.r2_score(y_true, y_pred) if len(y_true) > 1 else None, "mape": mape,
           "mape_rows_excluded": int((~nonzero).sum())}
    counts, edges = np.histogram(resid, bins=20) if len(resid) else (np.array([]), np.array([]))
    rng = np.random.RandomState(sample_seed)
    idx = rng.choice(len(y_true), size=min(len(y_true), 200), replace=False) if len(y_true) else []
    residuals = {"histogram": {"edges": edges.tolist(), "counts": counts.tolist()},
                 "points": [{"actual": float(y_true[i]), "predicted": float(y_pred[i])} for i in sorted(idx)],
                 "mean": float(resid.mean()) if len(resid) else None, "std": float(resid.std()) if len(resid) else None}
    return {"metrics": jsonable(out), "residuals": jsonable(residuals)}


def is_better(metric: str, a: float | None, b: float | None) -> bool:
    """True when value `a` beats value `b` on `metric` (None never wins)."""
    if a is None:
        return False
    if b is None:
        return True
    return a > b if HIGHER_IS_BETTER.get(metric, True) else a < b


def scorer_name(metric: str) -> str | None:
    """sklearn scoring string for cross-validation."""
    return {
        "accuracy": "accuracy", "f1_macro": "f1_macro", "f1_weighted": "f1_weighted", "roc_auc": "roc_auc",
        "pr_auc": "average_precision", "log_loss": "neg_log_loss", "precision_macro": "precision_macro",
        "recall_macro": "recall_macro", "mae": "neg_mean_absolute_error", "rmse": "neg_root_mean_squared_error",
        "r2": "r2", "mape": "neg_mean_absolute_percentage_error",
    }.get(metric)
