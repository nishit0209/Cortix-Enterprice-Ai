"""Validation of Gemini structured output against the OpenAPI-subset schema sent as `response_schema`.

Supported keywords (the subset Gemini accepts): type (object|array|string|integer|number|boolean),
properties, required, items, enum, nullable, minItems, maxItems, minLength, maxLength, minimum, maximum,
anyOf. Unknown properties are allowed unless `additionalProperties: false`.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S | re.I)


class OutputParseError(ValueError):
    pass


def parse_json_output(text: str | None) -> Any:
    """Parse model text as JSON (tolerating a ```json fence). Raises OutputParseError."""
    if text is None or not text.strip():
        raise OutputParseError("empty output")
    raw = text.strip()
    m = _FENCE_RE.match(raw)
    if m:
        raw = m.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OutputParseError(f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def validate(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []
    if instance is None:
        if schema.get("nullable") or schema.get("type") == "null":
            return errors
        if "anyOf" in schema and any(not validate(None, s, path) for s in schema["anyOf"]):
            return errors
        return [f"{path}: must not be null"]
    if "anyOf" in schema:
        branches = [validate(instance, s, path) for s in schema["anyOf"]]
        if not any(not b for b in branches):
            errors.append(f"{path}: does not match any allowed shape")
        return errors
    typ = str(schema.get("type", "")).lower()
    if typ:
        py_types = _TYPES.get(typ)
        if py_types is None:
            return [f"{path}: unsupported schema type {typ!r}"]
        ok = isinstance(instance, py_types) and not (typ in ("integer", "number") and isinstance(instance, bool))
        if typ == "integer" and isinstance(instance, float) and instance.is_integer():
            ok = True
        if not ok:
            return [f"{path}: expected {typ}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}.{req}: is required")
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unexpected property")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}[{i}]"))
    return errors
