"""Logical (user-facing) SQL table names of datasets: what NL->SQL, manual SQL and the semantic layer use.

The logical name is derived from the dataset slug: lower-case snake_case ASCII, a safe unquoted PostgreSQL
identifier (never a reserved word, never pg_*), at most 63 characters. The SQL guard maps it to the physical
"<org schema>.ds_<id>_v<n>" table of the version being queried.
"""

from __future__ import annotations

import re

# PostgreSQL reserved key words (SQL Key Words appendix, "reserved" + "reserved (can be function or type)").
PG_RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both case cast check collate collation
column concurrently constraint create cross current_catalog current_date current_role current_schema current_time
current_timestamp current_user default deferrable desc distinct do else end except false fetch for foreign freeze
from full grant group having ilike in initially inner intersect into is isnull join lateral leading left like limit
localtime localtimestamp natural not notnull null offset on only or order outer overlaps placing primary references
returning right select session_user similar some symmetric system_user table tablesample then to trailing true union
unique user using values variadic verbose when where window with
""".split())

_NON_IDENT = re.compile(r"[^a-z0-9_]+")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_LEN = 63


def logical_table_name(slug: str) -> str:
    name = _NON_IDENT.sub("_", (slug or "").lower()).strip("_")
    name = re.sub(r"_+", "_", name) or "dataset"
    if name[0].isdigit() or name.startswith("pg_") or name.startswith("information_schema"):
        name = f"t_{name}"
    name = name[:MAX_LEN].rstrip("_")
    if name in PG_RESERVED:
        name = f"{name}_data"
    return name


def is_safe_identifier(name: str) -> bool:
    return bool(_IDENT.match(name or "")) and name not in PG_RESERVED and not name.startswith("pg_")
