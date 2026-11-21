"""Data and prediction drift with the Population Stability Index (ML_ARCHITECTURE §9).

    PSI = sum_i (a_i - e_i) * ln(a_i / e_i)       e = reference (training) share, a = recent share per bin
    shares are floored at EPS so empty bins stay finite.  < 0.1 stable, 0.1-0.2 moderate, > 0.2 significant (alert)

Reference stats are computed once from the training data and stored on the model version (bin edges / category
lists and their shares: aggregates only). Each prediction call keeps only per-feature COUNTS in those bins, never
the raw rows; the drift job sums counts over a window and compares.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .frames import coerce_series

EPS = 1e-4
N_BINS = 10
TOP_CATEGORIES = 20
OTHER = "__other__"
MISSING = "__missing__"
WARN_PSI = 0.1
ALERT_PSI = 0.2
MIN_ROWS_FOR_DRIFT = 50


def psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    """PSI between two distributions given as shares OR counts over the same bins."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    if e.shape != a.shape:
        raise ValueError("expected and actual must have the same bins")
    e = e / e.sum() if e.sum() > 0 else e
    a = a / a.sum() if a.sum() > 0 else a
    e = np.maximum(e, EPS)
    a = np.maximum(a, EPS)
    return float(np.sum((a - e) * np.log(a / e)))


def psi_status(value: float | None) -> str:
    if value is None:
        return "insufficient_data"
    return "alert" if value > ALERT_PSI else "warning" if value > WARN_PSI else "ok"


# ------------------------------------------------------------------------------------------ binning
def numeric_edges(values: pd.Series, n_bins: int = N_BINS) -> list[float]:
    v = values.dropna().to_numpy(dtype=float)
    if not len(v):
        return []
    qs = np.quantile(v, np.linspace(0, 1, n_bins + 1)[1:-1])
    return sorted({float(q) for q in qs if math.isfinite(q)})


def numeric_counts(values: pd.Series, edges: Sequence[float]) -> list[int]:
    """Counts in (-inf, e1], (e1, e2], ..., (ek, inf) plus a final missing bin."""
    v = values.to_numpy(dtype=float)
    missing = np.isnan(v)
    idx = np.searchsorted(np.asarray(edges, dtype=float), v[~missing], side="left")
    counts = np.bincount(idx, minlength=len(edges) + 1).tolist()
    return [int(c) for c in counts] + [int(missing.sum())]


def categorical_counts(values: pd.Series, categories: Sequence[str]) -> list[int]:
    """Counts per known category, then OTHER (unseen / rare), then MISSING."""
    pos = {c: i for i, c in enumerate(categories)}
    counts = [0] * (len(categories) + 2)
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            counts[-1] += 1
        else:
            counts[pos.get(str(v), len(categories))] += 1
    return counts


def _feature_reference(series: pd.Series, kind: str) -> dict:
    if kind in ("numeric", "boolean"):
        values = coerce_series(series, kind)
        edges = numeric_edges(values)
        return {"type": "numeric", "edges": edges, "counts": numeric_counts(values, edges)}
    if kind == "datetime":
        values = coerce_series(series, kind)
        days = (values - pd.Timestamp("1970-01-01")).dt.total_seconds() / 86400.0
        edges = numeric_edges(days)
        return {"type": "datetime", "edges": edges, "counts": numeric_counts(days, edges)}
    values = coerce_series(series, "categorical")
    cats = [str(c) for c in values.value_counts(dropna=True).head(TOP_CATEGORIES).index]
    return {"type": "categorical", "categories": cats, "counts": categorical_counts(values, cats)}


def _prediction_reference(predictions: np.ndarray, task: str) -> dict | None:
    if predictions is None or not len(predictions):
        return None
    s = pd.Series(predictions)
    if task in ("classification", "clustering"):
        return _feature_reference(s.astype(str), "categorical")
    return _feature_reference(s.astype(float), "numeric")


def reference_stats(X: pd.DataFrame, schema: Iterable[Mapping], *, predictions=None, task: str = "") -> dict:
    out = {"features": {c["name"]: _feature_reference(X[c["name"]], c["kind"]) for c in schema},
           "rows": int(len(X))}
    pred = _prediction_reference(predictions, task)
    if pred is not None:
        out["prediction"] = pred
    return out


def bin_counts(X: pd.DataFrame, reference: Mapping, *, predictions=None) -> dict:
    """Per-feature counts of new inputs in the reference bins (what a PredictionLog stores)."""
    feats = {}
    for name, ref in (reference.get("features") or {}).items():
        if name not in X.columns:
            continue
        if ref["type"] == "numeric":
            feats[name] = numeric_counts(coerce_series(X[name], "numeric"), ref["edges"])
        elif ref["type"] == "datetime":
            values = coerce_series(X[name], "datetime")
            days = (values - pd.Timestamp("1970-01-01")).dt.total_seconds() / 86400.0
            feats[name] = numeric_counts(days, ref["edges"])
        else:
            feats[name] = categorical_counts(coerce_series(X[name], "categorical"), ref["categories"])
    out = {"features": feats}
    pref = reference.get("prediction")
    if pref is not None and predictions is not None and len(predictions):
        s = pd.Series(predictions)
        out["prediction"] = (categorical_counts(s.astype(str), pref["categories"]) if pref["type"] == "categorical"
                             else numeric_counts(s.astype(float), pref["edges"]))
    return out


def merge_counts(items: Iterable[Mapping]) -> dict:
    total: dict = {"features": {}, "prediction": None}
    for item in items:
        for name, counts in (item.get("features") or {}).items():
            cur = total["features"].get(name)
            total["features"][name] = list(counts) if cur is None or len(cur) != len(counts) else \
                [a + b for a, b in zip(cur, counts, strict=True)]
        pred = item.get("prediction")
        if pred:
            cur = total["prediction"]
            total["prediction"] = list(pred) if cur is None or len(cur) != len(pred) else \
                [a + b for a, b in zip(cur, pred, strict=True)]
    return total


def drift_report(reference: Mapping, recent: Mapping, *, min_rows: int = MIN_ROWS_FOR_DRIFT) -> dict:
    """PSI per feature + prediction PSI, overall status and alert list."""
    features = {}
    alerts = []
    n_recent = 0
    for name, ref in (reference.get("features") or {}).items():
        counts = (recent.get("features") or {}).get(name)
        if counts is None or len(counts) != len(ref["counts"]):
            continue
        n_recent = max(n_recent, int(sum(counts)))
        if sum(counts) < min_rows:
            features[name] = {"psi": None, "status": "insufficient_data", "rows": int(sum(counts))}
            continue
        value = psi(ref["counts"], counts)
        status = psi_status(value)
        features[name] = {"psi": round(value, 6), "status": status, "rows": int(sum(counts))}
        if status == "alert":
            alerts.append({"kind": "feature_drift", "feature": name, "psi": round(value, 4),
                           "message": f"'{name}' has drifted (PSI {value:.2f} > {ALERT_PSI})."})
    pred_psi = None
    pref, pcounts = reference.get("prediction"), recent.get("prediction")
    if pref and pcounts and len(pcounts) == len(pref["counts"]) and sum(pcounts) >= min_rows:
        pred_psi = round(psi(pref["counts"], pcounts), 6)
        if pred_psi > ALERT_PSI:
            alerts.append({"kind": "prediction_drift", "psi": round(pred_psi, 4),
                           "message": f"The prediction distribution has shifted (PSI {pred_psi:.2f})."})
    values = [f["psi"] for f in features.values() if f["psi"] is not None]
    max_psi = max(values) if values else None
    overall = max([v for v in (max_psi, pred_psi) if v is not None], default=None)
    return {"features": features, "prediction_psi": pred_psi, "max_psi": max_psi, "rows": n_recent,
            "status": psi_status(overall), "alerts": alerts}
