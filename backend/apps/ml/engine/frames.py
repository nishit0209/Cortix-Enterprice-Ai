"""Column typing and value coercion shared by training and prediction.

Kinds:
  numeric      int/float (and numeric-looking strings)          -> median impute (+ scale for linear models)
  boolean      bool / yes-no style values                        -> 0/1 numeric
  categorical  low/medium-cardinality strings                    -> most_frequent impute + one-hot (top-k + other)
  datetime     dates/timestamps (and ISO-looking strings)        -> year, month, day, dow, doy, is_weekend, days
  text         long free text                                    -> excluded (not a feature)
  id           identifiers (all unique, or named *_id / id)       -> excluded (leakage guard)
  constant     a single distinct value                           -> excluded (no signal)
  empty        all values missing                                -> excluded
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

FEATURE_KINDS = ("numeric", "boolean", "categorical", "datetime")
EXCLUDED_KINDS = ("text", "id", "constant", "empty")

ID_NAME_RE = re.compile(r"(^|[_\-\s.])id$|^(uu|gu)id$", re.I)
CAMEL_ID_RE = re.compile(r"[a-z0-9]Id$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
TRUE_VALUES = {"true", "yes", "y", "t", "1"}
FALSE_VALUES = {"false", "no", "n", "f", "0"}
UNIQUE_ID_MIN_ROWS = 20
NEAR_UNIQUE_RATIO = 0.95
TEXT_MEAN_LEN = 40
MAX_CATEGORIES_LISTED = 50


def to_pandas(frame: Any) -> pd.DataFrame:
    """polars or pandas -> pandas (object columns for strings, datetime64 for dates)."""
    if isinstance(frame, pd.DataFrame):
        return frame.copy()
    try:
        import polars as pl
    except ImportError:  # pragma: no cover - polars is installed
        pl = None
    if pl is not None and isinstance(frame, pl.DataFrame):
        data = {}
        for name, s in zip(frame.columns, frame.get_columns(), strict=True):
            dtype = s.dtype
            if dtype == pl.Date or dtype == pl.Datetime or isinstance(dtype, pl.Datetime):
                data[name] = pd.to_datetime(pd.Series(s.cast(pl.Datetime("us")).to_list(), dtype="object"))
            elif dtype.is_numeric():
                data[name] = pd.Series(s.cast(pl.Float64).to_numpy(), dtype="float64") if dtype.is_float() \
                    else pd.Series(s.to_list(), dtype="Int64" if s.null_count() else "int64")
            elif dtype == pl.Boolean:
                data[name] = pd.Series(s.to_list(), dtype="object")
            elif isinstance(dtype, pl.Decimal):
                data[name] = pd.Series(s.cast(pl.Float64).to_numpy(), dtype="float64")
            else:
                data[name] = pd.Series([None if v is None else str(v) for v in s.to_list()], dtype="object")
        return pd.DataFrame(data, columns=list(frame.columns))
    return pd.DataFrame(frame)


def is_id_name(name: str) -> bool:
    return bool(ID_NAME_RE.search(name.strip()) or CAMEL_ID_RE.search(name.strip()))


def _non_null(series: pd.Series) -> pd.Series:
    return series[series.notna()]


def _looks_boolean(values: pd.Series) -> bool:
    lowered = {str(v).strip().lower() for v in values.unique()[:5]}
    return 1 <= len(lowered) <= 2 and lowered <= (TRUE_VALUES | FALSE_VALUES) and not lowered <= {"0", "1"}


def _looks_datetime(values: pd.Series) -> bool:
    sample = values.astype(str).head(200)
    return len(sample) > 0 and sample.map(lambda v: bool(DATE_RE.match(v.strip()))).mean() >= 0.95


def _looks_numeric(values: pd.Series) -> bool:
    converted = pd.to_numeric(values.astype(str).str.strip().str.replace(",", "", regex=False), errors="coerce")
    return converted.notna().mean() >= 0.98


def infer_kind(name: str, series: pd.Series) -> str:
    values = _non_null(series)
    n = len(values)
    if n == 0:
        return "empty"
    if pd.api.types.is_bool_dtype(series.dtype) or all(isinstance(v, bool | np.bool_) for v in values.head(50)):
        return "boolean" if values.nunique() > 1 else "constant"
    n_unique = int(values.nunique())
    if n_unique == 1:
        return "constant"
    if is_id_name(name):
        return "id"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series.dtype):
        integral = bool(np.all(np.mod(values.astype(float), 1) == 0))
        if integral and n_unique == n and n >= UNIQUE_ID_MIN_ROWS:
            return "id"
        return "numeric"
    text = values.astype(str)
    if _looks_boolean(text):
        return "boolean"
    if _looks_datetime(text):
        return "datetime"
    if _looks_numeric(text):
        converted = pd.to_numeric(text.str.strip().str.replace(",", "", regex=False), errors="coerce").dropna()
        integral = bool(np.all(np.mod(converted, 1) == 0))
        if integral and converted.nunique() == n and n >= UNIQUE_ID_MIN_ROWS:
            return "id"
        return "numeric"
    if n >= UNIQUE_ID_MIN_ROWS and n_unique >= NEAR_UNIQUE_RATIO * n:
        return "id"
    if text.str.len().mean() > TEXT_MEAN_LEN and n_unique > 50:
        return "text"
    return "categorical"


# ------------------------------------------------------------------------------------------------ coercion
def coerce_series(series: pd.Series, kind: str) -> pd.Series:
    """Values of one column as the dtype the preprocessing pipeline expects (missing -> NaN / NaT)."""
    if kind == "numeric":
        if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
            return series.astype("float64")
        cleaned = series.map(lambda v: v if v is None or isinstance(v, int | float | np.number)
                             else str(v).strip().replace(",", ""))
        return pd.to_numeric(cleaned, errors="coerce").astype("float64")
    if kind == "boolean":
        def to_bool(v):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return np.nan
            if isinstance(v, bool | np.bool_):
                return float(v)
            s = str(v).strip().lower()
            if s in TRUE_VALUES:
                return 1.0
            if s in FALSE_VALUES:
                return 0.0
            return np.nan
        return series.map(to_bool).astype("float64")
    if kind == "datetime":
        if pd.api.types.is_datetime64_any_dtype(series.dtype):
            out = series
        else:
            out = pd.to_datetime(series.astype(object).where(series.notna(), None), errors="coerce",
                                 format="mixed")
        if getattr(out.dt, "tz", None) is not None:
            out = out.dt.tz_convert("UTC").dt.tz_localize(None)
        return out.astype("datetime64[ns]")
    # categorical / anything else: strings, missing -> NaN (SimpleImputer's missing marker)
    def to_text(v):
        if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
            return np.nan
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return series.astype(object).map(to_text).astype(object)


def coerce_frame(df: pd.DataFrame, schema: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({c["name"]: coerce_series(df[c["name"]], c["kind"]) for c in schema}, index=df.index)


# --------------------------------------------------------------------------------------------- profiling
def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def column_profile(name: str, series: pd.Series, kind: str | None = None) -> dict:
    kind = kind or infer_kind(name, series)
    n = len(series)
    missing = int(series.isna().sum())
    out: dict[str, Any] = {
        "name": name, "kind": kind, "dtype": str(series.dtype), "missing": missing,
        "missing_pct": round(100.0 * missing / n, 2) if n else 0.0, "n_unique": int(series.nunique(dropna=True)),
    }
    if kind in ("numeric", "boolean"):
        values = coerce_series(series, kind).dropna()
        if len(values):
            out.update({"min": _finite(values.min()), "max": _finite(values.max()),
                        "mean": _finite(values.mean()), "median": _finite(values.median())})
    elif kind == "categorical":
        counts = coerce_series(series, kind).value_counts(dropna=True)
        out["top_values"] = [{"value": str(k), "count": int(v)} for k, v in counts.head(10).items()]
    elif kind == "datetime":
        values = coerce_series(series, kind).dropna()
        if len(values):
            out.update({"min": values.min().isoformat(), "max": values.max().isoformat()})
    return out


def profile_frame(df: pd.DataFrame) -> list[dict]:
    return [column_profile(c, df[c]) for c in df.columns]


def feature_schema(df: pd.DataFrame, features: Iterable[str], kinds: Mapping[str, str], *, top_k: int = 20) -> list[dict]:
    """What prediction inputs must look like: name, kind, allowed categories (seen in training), numeric range,
    and the baseline value used by ablation explanations (median / mode). Aggregates only, never rows."""
    schema = []
    for name in features:
        kind = kinds[name]
        values = coerce_series(df[name], kind)
        entry: dict[str, Any] = {"name": name, "kind": kind, "required": True,
                                 "nullable": bool(values.isna().any())}
        if kind in ("numeric", "boolean"):
            nonnull = values.dropna()
            entry.update({"min": _finite(nonnull.min()) if len(nonnull) else None,
                          "max": _finite(nonnull.max()) if len(nonnull) else None,
                          "baseline": _finite(nonnull.median()) if len(nonnull) else 0.0})
        elif kind == "categorical":
            counts = values.value_counts(dropna=True)
            entry["categories"] = [str(k) for k in counts.head(MAX_CATEGORIES_LISTED).index]
            entry["n_categories"] = int(len(counts))
            entry["top_k"] = top_k
            entry["baseline"] = str(counts.index[0]) if len(counts) else None
        elif kind == "datetime":
            nonnull = values.dropna()
            entry.update({"min": nonnull.min().isoformat() if len(nonnull) else None,
                          "max": nonnull.max().isoformat() if len(nonnull) else None,
                          "baseline": nonnull.sort_values().iloc[len(nonnull) // 2].isoformat()
                          if len(nonnull) else None})
        schema.append(entry)
    return schema


def jsonable(value: Any) -> Any:
    """NaN/inf -> None, numpy scalars -> python, timestamps -> ISO strings (recursively)."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [jsonable(v) for v in value.tolist()]
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if value is pd.NaT or value is pd.NA:
        return None
    return value
