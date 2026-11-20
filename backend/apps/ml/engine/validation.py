"""Prediction input validation against the model's feature schema (ML_ARCHITECTURE §8).

* rows: a JSON object or a list of 1..1000 objects;
* every schema column must be present (a missing KEY is an error; a null VALUE is allowed and imputed);
* unknown columns are rejected (strict API) unless `allow_extra` (predict_frame passes extra columns through);
* values must parse as the column kind; unseen categories are fine (encoded as "unknown").
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from .frames import DATE_RE, FALSE_VALUES, TRUE_VALUES

MAX_ROWS = 1000
MAX_ERRORS = 50


class PredictionInputError(ValueError):
    def __init__(self, code: str, message: str, *, errors: list[dict] | None = None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = errors or []
        self.extra = extra


def _check_value(value: Any, kind: str) -> str | None:
    if value is None:
        return None
    if kind == "numeric":
        if isinstance(value, bool):
            return "invalid_number"
        if isinstance(value, int | float):
            return None if not (isinstance(value, float) and math.isinf(value)) else "invalid_number"
        try:
            float(str(value).replace(",", "").strip())
        except ValueError:
            return "invalid_number"
        return None
    if kind == "boolean":
        if isinstance(value, bool) or value in (0, 1):
            return None
        return None if str(value).strip().lower() in TRUE_VALUES | FALSE_VALUES else "invalid_boolean"
    if kind == "datetime":
        return None if isinstance(value, str) and DATE_RE.match(value.strip()) else "invalid_datetime"
    if isinstance(value, dict | list):
        return "invalid_category"
    return None


def validate_rows(rows: Any, schema: Sequence[Mapping], *, allow_extra: bool = False,
                  max_rows: int = MAX_ROWS) -> pd.DataFrame:
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise PredictionInputError("invalid_rows", "Send `rows` as a non-empty list of objects.")
    if len(rows) > max_rows:
        raise PredictionInputError("too_many_rows", f"At most {max_rows} rows per request; use batch predictions.",
                                   max_rows=max_rows)
    names = [c["name"] for c in schema]
    kinds = {c["name"]: c["kind"] for c in schema}
    missing: set[str] = set()
    unknown: set[str] = set()
    errors: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PredictionInputError("invalid_rows", f"rows[{i}] is not an object.")
        missing.update(n for n in names if n not in row)
        if not allow_extra:
            unknown.update(k for k in row if k not in kinds)
        for name in names:
            if name in row:
                code = _check_value(row[name], kinds[name])
                if code and len(errors) < MAX_ERRORS:
                    errors.append({"field": f"rows[{i}].{name}", "code": code,
                                   "message": f"Expected a {kinds[name]} value."})
    if missing:
        raise PredictionInputError("missing_columns", "Some feature columns are missing.",
                                   missing=sorted(missing), errors=[{"field": m, "code": "required",
                                                                     "message": "This feature is required."}
                                                                    for m in sorted(missing)])
    if unknown:
        raise PredictionInputError("unknown_columns", "Some columns are not features of this model.",
                                   unknown=sorted(unknown), errors=[{"field": u, "code": "unknown_column",
                                                                     "message": "Not a model feature."}
                                                                    for u in sorted(unknown)])
    if errors:
        raise PredictionInputError("invalid_values", "Some values do not match the feature types.", errors=errors)
    frame = pd.DataFrame([{k: row.get(k) for k in (names + ([k for k in row if k not in kinds]
                                                            if allow_extra else []))} for row in rows])
    return frame


def validate_frame(frame: pd.DataFrame, schema: Sequence[Mapping]) -> None:
    missing = [c["name"] for c in schema if c["name"] not in frame.columns]
    if missing:
        raise PredictionInputError("missing_columns", "Some feature columns are missing.", missing=missing)
