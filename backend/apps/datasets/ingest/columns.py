"""Column-name normalization: snake_case ASCII, unique, <= 63 chars, safe as PostgreSQL identifiers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

MAX_LEN = 63
# PostgreSQL system column names cannot be used for user columns.
SYSTEM_COLUMNS = {"oid", "tableoid", "xmin", "xmax", "cmin", "cmax", "ctid"}

_CAMEL_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SYMBOLS = {"%": " pct ", "#": " num ", "&": " and ", "@": " at ", "₹": " inr ", "$": " usd ", "€": " eur ", "£": " gbp "}


def to_snake(label: str) -> str:
    text = str(label or "")
    for sym, word in _SYMBOLS.items():
        text = text.replace(sym, word)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = _CAMEL_1.sub(r"\1_\2", text)
    text = _CAMEL_2.sub(r"\1_\2", text)
    text = _NON_ALNUM.sub("_", text.lower()).strip("_")
    return text


def _truncate(name: str, limit: int = MAX_LEN) -> str:
    if len(name) <= limit:
        return name
    return name[:limit].rstrip("_") or name[:limit]


def normalize_column_names(labels: Sequence[str]) -> list[str]:
    """Normalize header labels into unique PostgreSQL-safe column names (order preserved)."""
    out: list[str] = []
    seen: set[str] = set()
    for i, label in enumerate(labels):
        base = to_snake(label)
        if not base:
            base = f"column_{i + 1}"
        if base[0].isdigit():
            base = f"c_{base}"
        if base in SYSTEM_COLUMNS:
            base = f"{base}_"
        base = _truncate(base)
        name = base
        n = 2
        while name in seen:
            suffix = f"_{n}"
            name = _truncate(base, MAX_LEN - len(suffix)) + suffix
            n += 1
        seen.add(name)
        out.append(name)
    return out
