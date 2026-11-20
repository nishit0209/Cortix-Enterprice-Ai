"""Pure helpers for result sets: JSON conversion, size caps, chart choice and spreadsheet-safe export cells.

No database, no network: everything here is unit-testable on plain values.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from nexora.contracts import ChartHint, ColumnMeta, ResultSet

MAX_STORED_ROWS = 1000  # rows kept on a Message / sent inline in the `result` event
MAX_STORED_BYTES = 1_000_000  # approximate JSON size cap for a stored result
CHART_TYPES = frozenset({"bar", "line", "area", "pie", "scatter", "table", "kpi"})

_NUMERIC_TYPES = ("int", "numeric", "decimal", "double", "real", "float", "money", "serial")
_TEMPORAL_TYPES = ("date", "time", "timestamp", "interval")
_SHARE_NAME = re.compile(r"(share|pct|percent|percentage|proportion|ratio|mix|distribution|split)", re.I)
# Spreadsheet formula triggers (SECURITY_ARCHITECTURE §7): prefix with a single quote.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------------------------- JSON values
def jsonable(value: Any) -> Any:
    """Convert a database value into a JSON-safe value (no information beyond the value itself)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        as_int = int(value)
        return as_int if value == as_int and abs(as_int) < 2**53 else float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(v) for v in value]
    return str(value)


def column_dicts(columns: Iterable[ColumnMeta]) -> list[dict]:
    return [{"name": c.name, "type": c.type, "masked": bool(c.masked)} for c in columns]


def _approx_size(row: Sequence) -> int:
    return sum(len(str(v)) + 3 for v in row) + 2


def serialize_result(rs: ResultSet, *, max_rows: int = MAX_STORED_ROWS, max_bytes: int = MAX_STORED_BYTES) -> dict:
    """{columns, rows, row_count, truncated}. `rows` is capped by count and approximate JSON size;
    `truncated` is true when the executor or this cap cut rows. `row_count` is the executor's row count."""
    rows: list[list] = []
    size = 0
    cut = False
    for row in rs.rows:
        if len(rows) >= max_rows:
            cut = True
            break
        converted = [jsonable(v) for v in row]
        size += _approx_size(converted)
        if size > max_bytes and rows:
            cut = True
            break
        rows.append(converted)
    return {
        "columns": column_dicts(rs.columns),
        "rows": rows,
        "row_count": int(rs.row_count),
        "truncated": bool(rs.truncated or cut),
    }


def result_bytes(rs: ResultSet) -> int:
    return sum(_approx_size(r) for r in rs.rows)


# --------------------------------------------------------------------------------------------- chart choice
def _is_number(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def _is_temporal_value(v: Any) -> bool:
    if isinstance(v, datetime | date):
        return True
    return isinstance(v, str) and bool(re.match(r"^\d{4}-\d{2}(-\d{2})?([T ]\d{2}:\d{2}.*)?$", v))


def _kind(col: dict, values: list) -> str:
    t = (col.get("type") or "").lower()
    if any(k in t for k in _TEMPORAL_TYPES):
        return "temporal"
    if any(k in t for k in _NUMERIC_TYPES):
        return "numeric"
    present = [v for v in values if v is not None]
    if present and all(_is_number(v) for v in present):
        return "numeric"
    if present and all(_is_temporal_value(v) for v in present):
        return "temporal"
    return "categorical"


def _share_like(name: str, values: list) -> bool:
    nums = [float(v) for v in values if _is_number(v)]
    if not nums or any(v < 0 for v in nums):
        return False
    if _SHARE_NAME.search(name or ""):
        return True
    total = sum(nums)
    return math.isclose(total, 100.0, rel_tol=0.02) or math.isclose(total, 1.0, rel_tol=0.02)


def _hint_dict(hint: ChartHint | dict | None) -> dict | None:
    if hint is None:
        return None
    if isinstance(hint, dict):
        return {"type": hint.get("type"), "x": hint.get("x"), "y": list(hint.get("y") or ()), "series": hint.get("series")}
    return {"type": hint.type, "x": hint.x, "y": list(hint.y or ()), "series": hint.series}


def choose_chart(hint: ChartHint | dict | None, columns: list[dict], rows: list[list], *, question: str = "") -> dict | None:
    """A chart spec {type, x, y: [..], series} for the result, or None for an empty result.

    The model's hint is used only when every column it names exists in the result (and y columns are not
    masked); otherwise heuristics: 1 date/time column + numeric -> line; 1 category + numeric -> bar (pie when
    <= 6 rows and share-like); a single value -> kpi; anything else -> table."""
    if not columns:
        return None
    names = [c["name"] for c in columns]
    masked = {c["name"] for c in columns if c.get("masked")}
    h = _hint_dict(hint)
    if h and h["type"] in CHART_TYPES:
        refs = [r for r in [h["x"], h["series"], *h["y"]] if r]
        if all(r in names for r in refs) and not (set(h["y"]) & masked):
            if h["type"] in ("table", "kpi") or (h["x"] and h["y"]):
                return {"type": h["type"], "x": h["x"], "y": h["y"], "series": h["series"]}
    if not rows:
        return {"type": "table", "x": None, "y": [], "series": None}
    sample = rows[:200]
    kinds = {c["name"]: _kind(c, [r[i] if i < len(r) else None for r in sample]) for i, c in enumerate(columns)}
    numeric = [n for n in names if kinds[n] == "numeric" and n not in masked]
    temporal = [n for n in names if kinds[n] == "temporal"]
    categorical = [n for n in names if kinds[n] == "categorical"]
    if len(rows) == 1 and numeric and len(numeric) == len(names):
        return {"type": "kpi", "x": None, "y": numeric, "series": None}
    if len(temporal) == 1 and numeric:
        series = categorical[0] if len(categorical) == 1 else None
        return {"type": "line", "x": temporal[0], "y": numeric[:3] if series is None else numeric[:1], "series": series}
    if not temporal and len(categorical) >= 1 and numeric:
        x = categorical[0]
        series = categorical[1] if len(categorical) == 2 else None
        if series is None and len(rows) <= 6 and len(numeric) == 1:
            idx = names.index(numeric[0])
            if _share_like(numeric[0], [r[idx] for r in rows]) or "share" in question.lower():
                return {"type": "pie", "x": x, "y": numeric[:1], "series": None}
        return {"type": "bar", "x": x, "y": numeric[:1] if series else numeric[:3], "series": series}
    return {"type": "table", "x": None, "y": [], "series": None}


def chart_hint_from_dict(data: dict | None) -> ChartHint | None:
    if not data or data.get("type") not in CHART_TYPES:
        return None
    return ChartHint(type=data["type"], x=data.get("x"), y=tuple(data.get("y") or ()), series=data.get("series"))


# --------------------------------------------------------------------------------------------- export cells
def escape_formula(value: str) -> str:
    """Prefix spreadsheet formula triggers (= + - @ TAB CR) with a single quote (formula injection)."""
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGERS):
        return "'" + value
    return value


def csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        v = jsonable(value)
        return "" if v is None else v
    return escape_formula(str(jsonable(value)) if not isinstance(value, str) else value)


def xlsx_cell(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if _is_number(value):
        return jsonable(value)
    if isinstance(value, datetime):
        return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)  # Excel has no zones: UTC
    if isinstance(value, date):
        return value
    text = value if isinstance(value, str) else str(jsonable(value))
    return escape_formula(_ILLEGAL_XLSX.sub("", text))
