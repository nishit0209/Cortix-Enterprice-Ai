"""Deterministic, local generation of a dataset's semantic layer from its profiled columns (no Gemini, no values).

    column_spec(col: dict) -> GeneratedColumn          role, display name, description, synonyms, default agg
    default_metrics(table, columns, dataset_name)      COUNT(*) plus SUM / AVG of measures
    schema_hash(tables, columns)                       sha256 over logical names, column names and types

Descriptions and synonyms are SCHEMA-only text (they may be sent to Gemini under a schema policy): they never
contain data values such as min/max or top values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from apps.datasets.naming import PG_RESERVED

ROLES = ("dimension", "measure", "time", "id", "pii")
DEFAULT_AGGS = ("sum", "avg", "min", "max", "count", "count_distinct")
NUMERIC_TYPES = frozenset({"bigint", "integer", "smallint", "double precision", "numeric", "real"})
TIME_TYPES = frozenset({"date", "timestamptz", "timestamp"})
MAX_SYNONYMS = 8
MAX_DEFAULT_MEASURES = 25

# Business synonym groups: a token in a column name is expanded to the other members of its group.
SYNONYM_GROUPS: tuple[frozenset[str], ...] = tuple(frozenset(g.split()) for g in (
    "revenue sales turnover income",
    "region zone territory area",
    "customer client buyer",
    "product item sku article",
    "quantity qty units volume",
    "price rate",
    "discount rebate markdown",
    "profit margin earnings",
    "category class",
    "city town",
    "country nation",
    "state province",
    "order purchase transaction",
    "amount value total",
    "employee staff worker",
    "supplier vendor",
    "store shop outlet branch",
    "cost expense spend",
    "date day",
    "channel medium",
))
_GROUP_OF = {word: group for group in SYNONYM_GROUPS for word in group}
_AVG_HINTS = re.compile(r"(price|rate|ratio|pct|percent|percentage|score|rating|age|avg|average|mean|share)")
_ID_NAME = re.compile(r"(^id$|_id$|_key$|_uuid$|^uuid$)")
_TIME_PART_NAMES = frozenset({"year", "month", "week", "day", "quarter", "hour", "weekday", "fiscal_year"})
_DIMENSION_SEMANTICS = frozenset({"category", "boolean", "geo_country", "geo_city", "url", "text", "free_text"})


@dataclass(frozen=True)
class GeneratedColumn:
    name: str
    ordinal: int
    data_type: str
    semantic_type: str | None
    is_pii: bool
    role: str
    display_name: str
    description: str
    synonyms: tuple[str, ...] = field(default_factory=tuple)
    default_agg: str | None = None


def words(name: str) -> list[str]:
    return [w for w in re.split(r"[_\s]+", (name or "").lower()) if w]


def humanize(name: str) -> str:
    parts = words(name)
    return " ".join(p.upper() if p in ("id", "sku", "gst", "pan", "url") else p.capitalize() for p in parts) or name


def infer_role(name: str, data_type: str, semantic_type: str | None, is_pii: bool) -> str:
    sem = (semantic_type or "").lower()
    lname = (name or "").lower()
    if is_pii:
        return "pii"
    if sem == "id" or _ID_NAME.search(lname):
        return "id"
    if data_type in TIME_TYPES or sem == "date":
        return "time"
    if data_type in NUMERIC_TYPES:
        if sem in _DIMENSION_SEMANTICS or lname in _TIME_PART_NAMES or lname.endswith("_year"):
            return "dimension"
        return "measure"
    return "dimension"


def default_agg(name: str, role: str, semantic_type: str | None) -> str | None:
    if role == "measure":
        if (semantic_type or "") == "percent" or _AVG_HINTS.search(name.lower()):
            return "avg"
        return "sum"
    if role == "id":
        return "count_distinct"
    return None


def synonyms_for(name: str, source_name: str | None = None) -> tuple[str, ...]:
    tokens = words(name)
    out: list[str] = []

    def add(value: str) -> None:
        v = re.sub(r"\s+", " ", value.strip().lower())
        if v and v != name.lower() and v not in out and len(v) <= 80:
            out.append(v)

    if len(tokens) > 1:
        add(" ".join(tokens))
    if source_name and source_name.strip() and source_name.strip().lower() != name.lower():
        cleaned = re.sub(r"[^\w\s%()₹$€£/-]", " ", source_name, flags=re.UNICODE)
        add(cleaned)
    for i, tok in enumerate(tokens):
        for alt in sorted(_GROUP_OF.get(tok, frozenset()) - {tok}):
            add(" ".join(tokens[:i] + [alt] + tokens[i + 1:]))
    return tuple(out[:MAX_SYNONYMS])


def describe(col: Mapping, role: str, agg: str | None) -> str:
    display = humanize(col["name"])
    sem = col.get("semantic_type")
    dtype = col.get("data_type") or "text"
    if role == "pii":
        text = f"{display}: personal data ({col.get('pii_type') or sem or 'pii'}); aliased or excluded in AI prompts."
    elif role == "id":
        text = f"{display}: identifier; count distinct values instead of adding them up."
    elif role == "time":
        text = f"{display}: {'date' if dtype == 'date' else 'timestamp'} for time series and period filters."
    elif role == "measure":
        kind = f" ({sem})" if sem and sem not in ("number",) else ""
        text = f"{display}: numeric measure{kind}; aggregate with {(agg or 'sum').upper()}."
    else:
        kind = sem.replace("_", " ") if sem and sem not in ("text",) else "category"
        text = f"{display}: {kind} attribute for grouping and filtering."
    source = (col.get("source_name") or "").strip()
    if source and source.lower() != col["name"].lower() and len(source) <= 120:
        text += f" Source column: '{source}'."
    return text


def column_spec(col: Mapping) -> GeneratedColumn:
    name = col["name"]
    is_pii = bool(col.get("is_pii"))
    role = infer_role(name, col.get("data_type") or "text", col.get("semantic_type"), is_pii)
    agg = default_agg(name, role, col.get("semantic_type"))
    return GeneratedColumn(
        name=name,
        ordinal=int(col.get("ordinal") or 0),
        data_type=col.get("data_type") or "text",
        semantic_type=col.get("semantic_type"),
        is_pii=is_pii,
        role=role,
        display_name=humanize(name),
        description=describe(col, role, agg),
        synonyms=() if is_pii else synonyms_for(name, col.get("source_name")),
        default_agg=agg,
    )


def sql_ident(name: str) -> str:
    """Unquoted when safe, otherwise double-quoted (column names are snake ASCII; reserved words get quotes)."""
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and name not in PG_RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def default_metrics(table: str, columns: Sequence[GeneratedColumn], dataset_name: str = "") -> list[dict]:
    label = (dataset_name or table).strip()
    metrics = [{
        "key": "row_count",
        "name": "Row count",
        "expression": "COUNT(*)",
        "description": f"Number of rows in {label}.",
        "synonyms": ["number of rows", "number of records", "record count"],
        "table": table,
    }]
    measures = [c for c in columns if c.role == "measure" and not c.is_pii][:MAX_DEFAULT_MEASURES]
    for col in measures:
        phrase = " ".join(words(col.name))
        alts = [s for s in col.synonyms if s != phrase][:2]
        ident = sql_ident(col.name)
        if col.default_agg != "avg":
            metrics.append({
                "key": f"total_{col.name}"[:63],
                "name": f"Total {col.display_name}",
                "expression": f"SUM({ident})",
                "description": f"Sum of {col.display_name}.",
                "synonyms": [f"total {phrase}", f"sum of {phrase}", *(f"total {a}" for a in alts)],
                "table": table,
            })
        metrics.append({
            "key": f"avg_{col.name}"[:63],
            "name": f"Average {col.display_name}",
            "expression": f"AVG({ident})",
            "description": f"Average {col.display_name} per row.",
            "synonyms": [f"average {phrase}", f"mean {phrase}", f"avg {phrase}", *(f"average {a}" for a in alts)],
            "table": table,
        })
    return metrics


def schema_hash(tables: Mapping[str, str], columns: Mapping[str, Iterable[tuple[str, str]]]) -> str:
    canonical = {
        "tables": sorted(tables),
        "columns": {t: [list(c) for c in cols] for t, cols in sorted(columns.items())},
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def metric_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    if not key or key[0].isdigit():
        key = f"m_{key}"
    return key[:63]
