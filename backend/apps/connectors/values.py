"""Conversion of database values to CSV text / JSON-safe values (shared by imports and pipelines)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any


def to_text(value: Any) -> str | None:
    """Render one value as CSV text. None stays None (written as an empty, unquoted field)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value == value else ""  # NaN -> empty
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC)
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value.total_seconds())
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)
