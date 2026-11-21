"""Leakage guard (ML_ARCHITECTURE §5): identifiers and near-copies of the target never become features.

Dropped with a reason:
  id_like                 all values unique (integers/strings), or the name is `id` / ends with `_id`
  identical_to_target     equal to the target on every row, or a one-to-one relabelling of it
  near_duplicate_of_target |Pearson r| with the target > 0.98 (numeric / boolean features)
Other non-features are listed too: text (long free text), constant, empty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from .frames import coerce_series, infer_kind

NEAR_DUPLICATE_CORR = 0.98

REASON_TEXT = {
    "id_like": "Looks like an identifier (unique per row or named like an id); it cannot generalise.",
    "identical_to_target": "Carries the same information as the target (target leakage).",
    "near_duplicate_of_target": "Almost perfectly correlated with the target (|r| > 0.98): likely target leakage.",
    "text": "Long free text; text columns are excluded from tabular models.",
    "constant": "Has a single value; it carries no signal.",
    "empty": "Every value is missing.",
    "is_target": "This is the target column.",
    "is_time_column": "This is the time column of the forecast.",
}


def _target_numeric(y: pd.Series) -> pd.Series | None:
    """The target as numbers when that is meaningful (numeric, boolean, or exactly two classes)."""
    if pd.api.types.is_numeric_dtype(y.dtype) and not pd.api.types.is_bool_dtype(y.dtype):
        return y.astype("float64")
    values = y.dropna().astype(str)
    classes = sorted(values.unique())
    if len(classes) == 2:
        return y.map(lambda v: np.nan if pd.isna(v) else float(str(v) == classes[1])).astype("float64")
    numeric = pd.to_numeric(y, errors="coerce")
    return numeric.astype("float64") if numeric.notna().mean() >= 0.98 else None


def _one_to_one(a: pd.Series, b: pd.Series) -> bool:
    pair = pd.DataFrame({"a": a.astype(str), "b": b.astype(str)})[a.notna() & b.notna()]
    if len(pair) < 2 or pair["a"].nunique() < 2:
        return False
    return bool(pair.groupby("a")["b"].nunique().max() == 1 and pair.groupby("b")["a"].nunique().max() == 1)


def check_target_copy(feature: pd.Series, kind: str, target: pd.Series) -> tuple[str, float | None] | None:
    """('identical_to_target' | 'near_duplicate_of_target', |r|) or None."""
    mask = feature.notna() & target.notna()
    if mask.sum() < 3:
        return None
    if (feature[mask].astype(str).values == target[mask].astype(str).values).all():
        return "identical_to_target", 1.0
    if kind in ("numeric", "boolean"):
        y = _target_numeric(target)
        x = coerce_series(feature, kind)
        if y is not None:
            both = x.notna() & y.notna()
            if both.sum() >= 3 and x[both].std() > 0 and y[both].std() > 0:
                r = float(np.corrcoef(x[both], y[both])[0, 1])
                if abs(r) > NEAR_DUPLICATE_CORR:
                    return "near_duplicate_of_target", round(abs(r), 4)
    if kind in ("categorical", "boolean", "numeric") and target.nunique() <= 50 and _one_to_one(feature, target):
        return "identical_to_target", 1.0
    return None


def leakage_guard(df: pd.DataFrame, target: str | None, candidates: Iterable[str],
                  kinds: Mapping[str, str] | None = None, *, time_column: str | None = None) -> dict:
    """Split candidate columns into usable features and dropped columns (with reasons).

    Returns {"features": [...], "dropped": [{column, reason, message, corr?}], "kinds": {col: kind}}."""
    kinds = dict(kinds or {})
    features: list[str] = []
    dropped: list[dict] = []
    y = df[target] if target else None
    for col in candidates:
        if col == target:
            continue
        if time_column and col == time_column:
            continue
        kind = kinds.get(col) or infer_kind(col, df[col])
        kinds[col] = kind
        if kind in ("id", "text", "constant", "empty"):
            reason = "id_like" if kind == "id" else kind
            dropped.append({"column": col, "reason": reason, "message": REASON_TEXT[reason]})
            continue
        if y is not None:
            hit = check_target_copy(df[col], kind, y)
            if hit is not None:
                reason, corr = hit
                dropped.append({"column": col, "reason": reason, "message": REASON_TEXT[reason], "corr": corr})
                continue
        features.append(col)
    return {"features": features, "dropped": dropped, "kinds": kinds}
