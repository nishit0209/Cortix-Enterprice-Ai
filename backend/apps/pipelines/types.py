"""Logical column types used by DAG validation (static schema inference) and the Polars steps.

The vocabulary matches the warehouse/ingest types: text, bigint, double precision, numeric, boolean, date,
timestamptz, jsonb. Cast targets accept friendly aliases (integer, float, timestamp, string, ...).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

TEXT, BIGINT, DOUBLE, NUMERIC, BOOLEAN, DATE, TIMESTAMP, JSONB = (
    "text", "bigint", "double precision", "numeric", "boolean", "date", "timestamptz", "jsonb",
)
UNKNOWN = "unknown"
LOGICAL_TYPES = (TEXT, BIGINT, DOUBLE, NUMERIC, BOOLEAN, DATE, TIMESTAMP, JSONB)
NUMERIC_TYPES = frozenset({BIGINT, DOUBLE, NUMERIC})
TEMPORAL_TYPES = frozenset({DATE, TIMESTAMP})

CAST_ALIASES = {
    "text": TEXT, "string": TEXT, "str": TEXT, "varchar": TEXT,
    "integer": BIGINT, "int": BIGINT, "bigint": BIGINT, "int64": BIGINT,
    "float": DOUBLE, "double": DOUBLE, "double precision": DOUBLE, "number": DOUBLE, "numeric": DOUBLE,
    "decimal": DOUBLE,
    "boolean": BOOLEAN, "bool": BOOLEAN,
    "date": DATE,
    "timestamp": TIMESTAMP, "timestamptz": TIMESTAMP, "datetime": TIMESTAMP,
}

Schema = dict[str, str]  # ordered: column -> logical type


def normalize_cast_type(name: str) -> str | None:
    return CAST_ALIASES.get(str(name).strip().lower())


def is_numeric(t: str) -> bool:
    return t in NUMERIC_TYPES or t == UNKNOWN


def is_text(t: str) -> bool:
    return t in (TEXT, UNKNOWN)


def is_temporal(t: str) -> bool:
    return t in TEMPORAL_TYPES or t == UNKNOWN


def polars_dtype(t: str) -> pl.DataType:
    return {
        TEXT: pl.String(), BIGINT: pl.Int64(), DOUBLE: pl.Float64(), NUMERIC: pl.Float64(), BOOLEAN: pl.Boolean(),
        DATE: pl.Date(), TIMESTAMP: pl.Datetime("us", "UTC"), JSONB: pl.String(),
    }.get(t, pl.String())


def logical_type(dtype: pl.DataType) -> str:
    """Logical type of a Polars dtype (used to report preview schemas)."""
    if dtype == pl.Boolean:
        return BOOLEAN
    if dtype.is_integer():
        return BIGINT
    if dtype.is_float() or dtype.is_decimal():
        return DOUBLE
    if dtype == pl.Date:
        return DATE
    if isinstance(dtype, pl.Datetime):
        return TIMESTAMP
    if dtype in (pl.String, pl.Categorical, pl.Enum) or isinstance(dtype, (pl.Categorical, pl.Enum)):
        return TEXT
    if isinstance(dtype, (pl.List, pl.Struct, pl.Array)):
        return JSONB
    return TEXT


def frame_schema(frame: pl.DataFrame) -> Schema:
    return {name: logical_type(dtype) for name, dtype in frame.schema.items()}


def coerce_literal(value: Any, dtype: pl.DataType) -> Any:
    """Turn a JSON literal into a Python value comparable with a column of `dtype` (dates from ISO strings)."""
    if value is None:
        return None
    if dtype == pl.Date and isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if isinstance(dtype, pl.Datetime) and isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        if dtype.time_zone:
            return parsed.astimezone(dt.UTC)
        return parsed.astimezone(dt.UTC).replace(tzinfo=None)
    if dtype == pl.Boolean and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "t", "yes", "y", "1"):
            return True
        if low in ("false", "f", "no", "n", "0"):
            return False
    return value


def literal_type(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int):
        return BIGINT
    if isinstance(value, float):
        return DOUBLE
    return TEXT
