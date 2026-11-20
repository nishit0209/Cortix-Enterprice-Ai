"""Local exploratory data analysis (ML_ARCHITECTURE §2). Aggregates only: no row values leave this module except
category labels with their counts (top values) and numeric summary statistics.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from .frames import coerce_series, jsonable

TASK_TYPES = ("classification", "regression", "forecast", "clustering")
MAX_CORR_COLUMNS = 20
HIST_BINS = 10
REGRESSION_MIN_UNIQUE = 15


def detect_task(df: pd.DataFrame, target: str | None, kinds: Mapping[str, str], *, time_column: str | None = None
                ) -> str:
    """No target -> clustering; time column + numeric target -> forecast; numeric target with many distinct values
    -> regression; anything else -> classification."""
    if not target:
        return "clustering"
    kind = kinds.get(target)
    if kind == "numeric":
        values = coerce_series(df[target], "numeric").dropna()
        if time_column:
            return "forecast"
        integral = bool(np.all(np.mod(values, 1) == 0)) if len(values) else True
        if values.nunique() > REGRESSION_MIN_UNIQUE or not integral:
            return "regression"
    return "classification"


def _histogram(values: pd.Series, bins: int = HIST_BINS) -> dict:
    values = values.dropna()
    if not len(values):
        return {"edges": [], "counts": []}
    counts, edges = np.histogram(values.to_numpy(dtype=float), bins=bins)
    return {"edges": jsonable(edges), "counts": jsonable(counts)}


def _numeric_stats(values: pd.Series) -> dict:
    v = values.dropna()
    if not len(v):
        return {}
    q1, q3 = v.quantile(0.25), v.quantile(0.75)
    iqr = q3 - q1
    std = v.std()
    z_out = int((((v - v.mean()) / std).abs() > 3).sum()) if std and std > 0 else 0
    return jsonable({
        "mean": v.mean(), "std": std, "min": v.min(), "p25": q1, "median": v.median(), "p75": q3, "max": v.max(),
        "skew": v.skew() if len(v) > 2 else None, "zeros": int((v == 0).sum()),
        "outliers_iqr": int(((v < q1 - 1.5 * iqr) | (v > q3 + 1.5 * iqr)).sum()), "outliers_z": z_out,
        "histogram": _histogram(v),
    })


def _missing_strategy(missing_pct: float, kind: str) -> str:
    if missing_pct > 60:
        return "drop_column"
    if missing_pct == 0:
        return "none"
    return "median" if kind in ("numeric", "boolean", "datetime") else "most_frequent"


def _cramers_v(a: pd.Series, b: pd.Series) -> float | None:
    from scipy.stats import chi2_contingency

    table = pd.crosstab(a.astype(str), b.astype(str))
    if table.shape[0] < 2 or table.shape[1] < 2:
        return None
    chi2 = chi2_contingency(table, correction=False)[0]
    n = table.to_numpy().sum()
    k = min(table.shape) - 1
    return float(math.sqrt(chi2 / (n * k))) if n and k else None


def _correlations(df: pd.DataFrame, numeric_cols: Sequence[str]) -> dict:
    cols = list(numeric_cols)[:MAX_CORR_COLUMNS]
    if len(cols) < 2:
        return {"columns": cols, "pearson": [], "spearman": []}
    data = pd.DataFrame({c: coerce_series(df[c], "numeric") for c in cols})
    return {"columns": cols, "pearson": jsonable(data.corr(method="pearson").round(4).to_numpy()),
            "spearman": jsonable(data.corr(method="spearman").round(4).to_numpy())}


def _target_section(df: pd.DataFrame, target: str, task: str, kinds: Mapping[str, str]) -> dict:
    y = df[target]
    out: dict[str, Any] = {"name": target, "missing": int(y.isna().sum())}
    if task == "classification":
        counts = y.dropna().astype(str).value_counts()
        total = int(counts.sum())
        out["classes"] = [{"value": str(k), "count": int(v), "share": round(v / total, 4) if total else 0.0}
                          for k, v in counts.items()]
        out["n_classes"] = int(len(counts))
        out["minority_share"] = round(float(counts.min() / total), 4) if total else None
        out["imbalance_ratio"] = round(float(counts.max() / counts.min()), 3) if len(counts) and counts.min() else None
        out["imbalanced"] = bool(out["minority_share"] is not None and out["minority_share"] < 0.10)
    else:
        out.update(_numeric_stats(coerce_series(y, "numeric")))
    return out


def _time_coverage(df: pd.DataFrame, time_column: str, target: str | None) -> dict:
    from .forecast import infer_freq, prepare_series

    ts = coerce_series(df[time_column], "datetime").dropna()
    if not len(ts):
        return {"column": time_column, "rows": 0}
    out: dict[str, Any] = {"column": time_column, "min": ts.min().isoformat(), "max": ts.max().isoformat(),
                           "rows": int(len(ts)), "distinct_timestamps": int(ts.nunique())}
    try:
        out["frequency"] = infer_freq(ts)
        if target:
            series, info = prepare_series(df, time_column, target, freq=out["frequency"])
            out["periods"], out["gaps"] = info["periods"], info["gaps"]
            if len(series) >= 12:
                from statsmodels.tsa.stattools import adfuller

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    p_value = float(adfuller(series.to_numpy(dtype=float), autolag="AIC")[1])
                out["adf_p_value"] = round(p_value, 4)
                out["stationary"] = p_value < 0.05
    except (ValueError, TypeError) as exc:
        out["warning"] = f"Time coverage could not be computed ({type(exc).__name__})."
    return jsonable(out)


def _isolation_forest_summary(df: pd.DataFrame, numeric_cols: Sequence[str], random_state: int) -> dict | None:
    if not numeric_cols or len(df) < 50:
        return None
    from sklearn.ensemble import IsolationForest

    data = pd.DataFrame({c: coerce_series(df[c], "numeric") for c in numeric_cols})
    data = data.fillna(data.median()).fillna(0.0)
    if len(data) > 50_000:
        data = data.sample(50_000, random_state=random_state)
    model = IsolationForest(n_estimators=100, contamination="auto", random_state=random_state)
    flags = model.fit_predict(data.to_numpy(dtype=float))
    n_flagged = int((flags == -1).sum())
    return {"method": "isolation_forest", "rows_checked": int(len(data)), "flagged": n_flagged,
            "flagged_share": round(n_flagged / len(data), 4)}


def compute_eda(df: pd.DataFrame, *, target: str | None, task: str, features: Sequence[str],
                kinds: Mapping[str, str], time_column: str | None = None, random_state: int = 42) -> dict:
    """The full EDA payload (JSON-ready). `df` holds the policy-allowed columns only."""
    n = len(df)
    columns = []
    numeric_cols = []
    warnings_out: list[str] = []
    for col in df.columns:
        kind = kinds.get(col, "categorical")
        series = df[col]
        missing = int(series.isna().sum())
        pct = round(100.0 * missing / n, 2) if n else 0.0
        entry: dict[str, Any] = {"name": col, "kind": kind, "missing": missing, "missing_pct": pct,
                                 "n_unique": int(series.nunique(dropna=True)), "is_feature": col in features,
                                 "is_target": col == target, "missing_strategy": _missing_strategy(pct, kind)}
        if kind in ("numeric", "boolean"):
            entry.update(_numeric_stats(coerce_series(series, kind)))
            if kind == "numeric":
                numeric_cols.append(col)
        elif kind == "categorical":
            values = coerce_series(series, kind)
            counts = values.value_counts(dropna=True)
            entry["top_values"] = [{"value": str(k), "count": int(v)} for k, v in counts.head(10).items()]
            entry["high_cardinality"] = bool(len(counts) > 30)
        elif kind == "datetime":
            values = coerce_series(series, kind).dropna()
            if len(values):
                entry.update({"min": values.min().isoformat(), "max": values.max().isoformat()})
        if pct > 60 and col in features:
            warnings_out.append(f"'{col}' is {pct}% missing; consider dropping it.")
        columns.append(jsonable(entry))

    out: dict[str, Any] = {"rows": n, "columns_count": len(df.columns), "columns": columns,
                           "duplicate_rows": int(df.astype(str).duplicated().sum()) if n else 0}
    if out["duplicate_rows"]:
        warnings_out.append(f"{out['duplicate_rows']} duplicate rows found.")
    corr_cols = [c for c in numeric_cols if c in features or c == target]
    out["correlations"] = _correlations(df, corr_cols)
    if target and task != "clustering":
        out["target"] = _target_section(df, target, task, kinds)
        if out["target"].get("imbalanced"):
            warnings_out.append("Strong class imbalance (minority < 10%): judge models by PR-AUC and F1, not accuracy.")
        assoc = []
        y = df[target]
        y_num = coerce_series(y, "numeric") if task in ("regression", "forecast") else None
        if task == "classification" and y.dropna().astype(str).nunique() == 2:
            classes = sorted(y.dropna().astype(str).unique())
            y_num = y.map(lambda v: np.nan if pd.isna(v) else float(str(v) == classes[1]))
        for col in features:
            kind = kinds.get(col)
            if kind in ("numeric", "boolean") and y_num is not None:
                x = coerce_series(df[col], kind)
                both = x.notna() & y_num.notna()
                if both.sum() > 2 and x[both].std() > 0 and y_num[both].std() > 0:
                    assoc.append({"feature": col, "method": "pearson",
                                  "value": round(float(np.corrcoef(x[both], y_num[both])[0, 1]), 4)})
            elif kind == "categorical" and task == "classification":
                v = _cramers_v(coerce_series(df[col], kind).fillna("__missing__"), y.fillna("__missing__"))
                if v is not None:
                    assoc.append({"feature": col, "method": "cramers_v", "value": round(v, 4)})
        out["target_associations"] = sorted(assoc, key=lambda a: -abs(a["value"] or 0))
    outliers = _isolation_forest_summary(df, [c for c in numeric_cols if c in features], random_state)
    if outliers:
        out["outliers"] = outliers
    if time_column and time_column in df.columns:
        out["time_coverage"] = _time_coverage(df, time_column, target if task == "forecast" else None)
    if n < 1000:
        warnings_out.append(f"Only {n} rows: results may be unreliable; prefer simple models.")
    out["warnings"] = warnings_out
    return jsonable(out)


def narration_facts(eda: dict, *, task: str, target: str | None) -> dict:
    """Schema + aggregates used by the optional narration (never row values): task, sizes, target balance,
    strongest associations, missingness."""
    target_info = eda.get("target") or {}
    return {
        "task": task, "target": target, "rows": eda.get("rows"), "columns": eda.get("columns_count"),
        "classes": [{"value": c["value"], "share": c["share"]} for c in target_info.get("classes", [])][:10],
        "top_associations": eda.get("target_associations", [])[:5],
        "missing": [{"column": c["name"], "missing_pct": c["missing_pct"]}
                    for c in eda.get("columns", []) if c.get("missing_pct")][:10],
        "warnings": eda.get("warnings", [])[:6],
    }


def template_narrative(eda: dict, *, task: str, target: str | None) -> str:
    """Local, deterministic narration (the default: no LLM)."""
    parts = [f"The dataset has {eda.get('rows', 0):,} rows and {eda.get('columns_count', 0)} columns."]
    t = eda.get("target") or {}
    if task == "classification" and t.get("classes"):
        top = ", ".join(f"{c['value']} ({c['share'] * 100:.1f}%)" for c in t["classes"][:4])
        parts.append(f"The target '{target}' has {t.get('n_classes')} classes: {top}.")
        if t.get("imbalanced"):
            parts.append("The classes are strongly imbalanced, so PR-AUC and F1 are better guides than accuracy.")
    elif task in ("regression", "forecast") and t.get("mean") is not None:
        parts.append(f"The target '{target}' averages {t['mean']:,.2f} (median {t.get('median', 0):,.2f}, "
                     f"range {t.get('min', 0):,.2f} to {t.get('max', 0):,.2f}).")
    elif task == "clustering":
        parts.append("There is no target: the workspace will look for natural groups (clusters).")
    assoc = eda.get("target_associations") or []
    if assoc:
        top = ", ".join(f"{a['feature']} ({a['method']} {a['value']:+.2f})" for a in assoc[:3])
        parts.append(f"The features most associated with the target are {top}.")
    missing = [c for c in eda.get("columns", []) if c.get("missing_pct")]
    if missing:
        worst = max(missing, key=lambda c: c["missing_pct"])
        parts.append(f"{len(missing)} column(s) have missing values; the worst is '{worst['name']}' "
                     f"({worst['missing_pct']}%), which will be imputed ({worst['missing_strategy']}).")
    else:
        parts.append("No values are missing.")
    tc = eda.get("time_coverage")
    if tc and tc.get("min"):
        parts.append(f"The time column covers {tc['min'][:10]} to {tc['max'][:10]}"
                     + (f" at a {tc['frequency']} frequency." if tc.get("frequency") else "."))
    return " ".join(parts)
