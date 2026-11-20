"""Preprocessing serialized INSIDE the model artifact (ML_ARCHITECTURE §3), so prediction-time transforms match
training exactly:

    Pipeline([
        ("coerce", FrameCoercer(schema)),          # dtypes per feature kind; extra columns dropped
        ("prep",   ColumnTransformer([...])),       # ONE transformer per input column (exact column attribution)
        ("model",  estimator),
    ])

Per kind: numeric/boolean -> median impute (+ standard scaling for scale-sensitive models); categorical ->
most_frequent impute + one-hot (top-k levels, the rest share an "infrequent" column; unknown levels at prediction
time encode as all zeros: handle_unknown="ignore"); datetime -> year, month, day, day of week, day of year,
is_weekend, days since the training minimum, then median impute (+ scaling). Text/id columns never get here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .frames import coerce_frame

DEFAULT_TOP_K = 20
DATETIME_PARTS = ("year", "month", "day", "dayofweek", "dayofyear", "is_weekend", "days")


class FrameCoercer(BaseEstimator, TransformerMixin):
    """Selects the schema's columns (in order) and coerces their dtypes. Stateless."""

    def __init__(self, schema: Sequence[Mapping] = ()):
        self.schema = schema

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        if not isinstance(X, pd.DataFrame):
            X = pd.DataFrame(X, columns=[c["name"] for c in self.schema])
        return coerce_frame(X, [{"name": c["name"], "kind": c["kind"]} for c in self.schema])


class DateTimeFeatures(BaseEstimator, TransformerMixin):
    """One datetime column -> calendar parts + days since the training minimum (NaT -> NaN)."""

    def fit(self, X, y=None):
        col = _first_column(X)
        valid = col.dropna()
        self.origin_ = valid.min() if len(valid) else pd.Timestamp("1970-01-01")
        self.name_ = str(X.columns[0]) if isinstance(X, pd.DataFrame) else "datetime"
        return self

    def transform(self, X):
        col = pd.to_datetime(_first_column(X), errors="coerce")
        out = np.column_stack([
            col.dt.year, col.dt.month, col.dt.day, col.dt.dayofweek, col.dt.dayofyear,
            (col.dt.dayofweek >= 5).astype(float).where(col.notna()),
            (col - self.origin_).dt.total_seconds() / 86400.0,
        ]).astype(float)
        return out

    def get_feature_names_out(self, input_features=None):
        name = (list(input_features)[0] if input_features is not None else self.name_)
        return np.array([f"{name}_{p}" for p in DATETIME_PARTS], dtype=object)


def _first_column(X) -> pd.Series:
    if isinstance(X, pd.DataFrame):
        return X.iloc[:, 0]
    return pd.Series(np.asarray(X)[:, 0])


def column_transformer(schema: Sequence[Mapping], *, scale: bool, top_k: int = DEFAULT_TOP_K) -> ColumnTransformer:
    transformers = []
    for i, col in enumerate(schema):
        name, kind = col["name"], col["kind"]
        if kind in ("numeric", "boolean"):
            steps = [("impute", SimpleImputer(strategy="median", keep_empty_features=True))]
            if scale:
                steps.append(("scale", StandardScaler()))
        elif kind == "datetime":
            steps = [("parts", DateTimeFeatures()),
                     ("impute", SimpleImputer(strategy="median", keep_empty_features=True))]
            if scale:
                steps.append(("scale", StandardScaler()))
        elif kind == "categorical":
            steps = [("impute", SimpleImputer(strategy="most_frequent", keep_empty_features=True)),
                     ("onehot", OneHotEncoder(handle_unknown="ignore", max_categories=top_k + 1,
                                              sparse_output=False, dtype=np.float64))]
        else:
            raise ValueError(f"Column {name!r} of kind {kind!r} cannot be a feature.")
        transformers.append((f"c{i}", Pipeline(steps), [name]))
    return ColumnTransformer(transformers, remainder="drop", sparse_threshold=0.0, verbose_feature_names_out=False)


def build_pipeline(schema: Sequence[Mapping], estimator, *, scale: bool, top_k: int = DEFAULT_TOP_K) -> Pipeline:
    return Pipeline([("coerce", FrameCoercer(list(schema))),
                     ("prep", column_transformer(schema, scale=scale, top_k=top_k)),
                     ("model", estimator)])


def column_slices(pipeline: Pipeline) -> dict[str, slice]:
    """Original column -> slice of the transformed matrix (one transformer per column: exact attribution)."""
    prep: ColumnTransformer = pipeline.named_steps["prep"]
    schema = pipeline.named_steps["coerce"].schema
    slices = {}
    for i, col in enumerate(schema):
        sl = prep.output_indices_.get(f"c{i}")
        if sl is not None and sl.stop > sl.start:
            slices[col["name"]] = sl
    return slices


def transformed_feature_names(pipeline: Pipeline) -> list[str]:
    return [str(n) for n in pipeline.named_steps["prep"].get_feature_names_out()]


def transform(pipeline: Pipeline, X: pd.DataFrame) -> np.ndarray:
    """The matrix the final estimator sees."""
    return np.asarray(pipeline[:-1].transform(X), dtype=float)


def aggregate_columns(values: np.ndarray, slices: Mapping[str, slice]) -> dict[str, np.ndarray]:
    """Sum transformed-feature values (e.g. SHAP contributions) back to their original columns (last axis)."""
    return {name: values[..., sl].sum(axis=-1) for name, sl in slices.items()}
