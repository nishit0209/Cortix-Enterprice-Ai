"""Dimension-value filters for the deterministic builder ("revenue in the North region", "online orders").

Two general rules, never question-specific:

1. KNOWN values. When the semantic layer lists a dimension's values in its column metadata (`values`,
   `top_values` or `sample_values`: strings or {"value": ...} dicts), a value mentioned in the question becomes
   `"col" = '<stored value>'`. Matching is case-insensitive on whole words, with a one-edit typo allowance for
   values of 5+ characters ("electroncs" -> Electronics). A value that matches several columns is resolved by the
   column name right after it ("north region"); otherwise the builder declines.
2. NAMED values. Without a value list, "<in|from|for|at|within> [the] <value> <dimension name>" ("in the North
   region", "for the online channel") becomes `LOWER("col") = '<value>'` (the stored casing is unknown).

The matched words (and a directly following column name and a leading preposition) are removed from the
question, so the remaining text is parsed by the normal patterns. PII and identifier columns are never used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz.distance import OSA

from .sqlutil import quote_ident

PREPOSITIONS = ("in", "from", "for", "at", "within", "of", "on", "to")
MAX_VALUE_WORDS = 4
MAX_VALUE_CHARS = 60
MAX_VALUES_PER_COLUMN = 500
# Words that can never be (part of) a filter value: grammar, aggregation and ranking vocabulary.
RESERVED = frozenset("""
a an the and or of in on at to for from by per with within without each every all any some our my your their
this last next previous prior current past same total sum average avg mean median count number how many much
what which who top bottom best worst highest lowest most least largest smallest biggest vs versus compare
compared against over under between since until before after during trend trends time series daily weekly
monthly quarterly yearly annual day days week weeks month months quarter quarters year years today yesterday
ytd mtd qtd show me list give get display plot chart graph is are was were do does did we us it its
""".split())


@dataclass(frozen=True)
class ValueFilter:
    table: str
    column: str
    value: str
    exact: bool  # True: a known stored value (equality); False: case-insensitive match on a named value
    phrase: str

    def sql(self) -> str:
        col = quote_ident(self.column)
        if self.exact:
            return f"{col} = {sql_string(self.value)}"
        return f"LOWER({col}) = {sql_string(self.value.lower())}"

    def describe(self) -> str:
        if self.exact:
            return f"Filtered to {self.column} = '{self.value}'"
        return f"Filtered to {self.column} = '{self.value}' (case-insensitive)"


def sql_string(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def column_values(meta: dict) -> tuple[str, ...]:
    """Distinct non-empty string values listed in a column's metadata (bounded)."""
    raw = None
    for key in ("values", "top_values", "sample_values", "enum"):
        if meta.get(key):
            raw = meta[key]
            break
    if not raw or not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw[:MAX_VALUES_PER_COLUMN]:
        value = item.get("value") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and len(value) <= MAX_VALUE_CHARS:
            out.append(value.strip())
    return tuple(dict.fromkeys(out))


def _words(text: str) -> list[str]:
    return re.findall(r"[\w&'-]+", text.lower())


def _norm_value(value: str) -> str:
    return " ".join(_words(value))


def _usable_value(norm: str, reserved_names: set[str]) -> bool:
    words = norm.split()
    if not words or len(words) > MAX_VALUE_WORDS:
        return False
    if all(w in RESERVED for w in words) or norm in reserved_names:
        return False
    return not re.fullmatch(r"[\d.,\s-]+", norm)  # numbers collide with "top 5", years, amounts


@dataclass
class _Span:
    start: int
    end: int
    flt: ValueFilter


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[\w&'-]+", text)]


def _column_name_at(tokens, idx: int, names: dict[str, set[str]]) -> tuple[str | None, int]:
    """Column whose name/synonym starts at token idx -> (column, number of tokens)."""
    for size in (3, 2, 1):
        if idx + size > len(tokens):
            continue
        phrase = " ".join(t[0] for t in tokens[idx:idx + size])
        for col, variants in names.items():
            if phrase in variants:
                return col, size
    return None, 0


def extract(text: str, columns, *, reserved_names: set[str]) -> tuple[str, list[ValueFilter], str | None]:
    """(text without the filter phrases, filters, decline reason or None). `text` is the normalized question.

    `columns`: iterable of objects with table, name, role, synonyms, values, is_pii, masked (builder.Column).
    PII and masked columns are never filtered on.
    """
    dims = [c for c in columns if c.role == "dimension" and not getattr(c, "is_pii", False)
            and not getattr(c, "masked", False)]
    if not dims:
        return text, [], None
    names: dict[str, set[str]] = {}
    for c in dims:
        variants = {_norm_value(c.name.replace("_", " "))} | {_norm_value(s) for s in c.synonyms}
        variants |= {v + "s" for v in list(variants) if v and not v.endswith("s")}
        variants |= {v[:-1] + "ies" for v in list(variants) if v.endswith("y")}
        names[c.name] = {v for v in variants if v}
    by_name = {c.name: c for c in dims}

    known: dict[str, list[tuple[object, str]]] = {}
    for c in dims:
        for value in getattr(c, "values", ()) or ():
            norm = _norm_value(value)
            if _usable_value(norm, reserved_names):
                known.setdefault(norm, []).append((c, value))

    tokens = _token_spans(text)
    spans: list[_Span] = []
    used = [False] * len(tokens)

    # 1. known values (longest first; exact before fuzzy)
    if known:
        max_words = max(len(k.split()) for k in known)
        for fuzzy in (False, True):
            for size in range(min(max_words, MAX_VALUE_WORDS), 0, -1):
                for i in range(len(tokens) - size + 1):
                    if any(used[i:i + size]):
                        continue
                    phrase = " ".join(t[0] for t in tokens[i:i + size])
                    if all(w in RESERVED for w in phrase.split()):
                        continue
                    cands = known.get(phrase) if not fuzzy else None
                    if fuzzy and len(phrase) >= 5:
                        hits = [k for k in known if len(k.split()) == size and abs(len(k) - len(phrase)) <= 1
                                and OSA.distance(k, phrase) <= 1]
                        cands = [x for k in hits for x in known[k]] if len(hits) == 1 else None
                    if not cands:
                        continue
                    col_after, n_after = _column_name_at(tokens, i + size, names)
                    if len({c.name for c, _ in cands}) > 1:
                        chosen = [x for x in cands if x[0].name == col_after]
                        if not chosen:
                            return text, [], f"value {phrase!r} matches several columns"
                        cands = chosen
                    col, value = cands[0]
                    end_idx = i + size + (n_after if col_after == col.name else 0)
                    for j in range(i, end_idx):
                        used[j] = True
                    spans.append(_Span(i, end_idx, ValueFilter(col.table, col.name, value, True, phrase)))

    # 2. named values: <prep> [the] <value words> <column name>
    i = 0
    while i < len(tokens):
        if used[i] or tokens[i][0] not in PREPOSITIONS:
            i += 1
            continue
        j = i + 1
        if j < len(tokens) and tokens[j][0] == "the":
            j += 1
        found = False
        for size in range(1, MAX_VALUE_WORDS + 1):
            k = j + size
            if k > len(tokens) or any(used[j:k]):
                break
            col_name, n_col = _column_name_at(tokens, k, names)
            if col_name is None:
                continue
            words = [t[0] for t in tokens[j:k]]
            value = " ".join(words)
            if any(w in RESERVED for w in words) or not _usable_value(value, reserved_names):
                break
            whole = " ".join(t[0] for t in tokens[j:k + n_col])
            if any(value == v or whole == v for vs in names.values() for v in vs) or value in reserved_names:
                break  # "on product category" is a column name, not "<value> <column>"
            col = by_name[col_name]
            for q in range(i, k + n_col):
                used[q] = True
            spans.append(_Span(i, k + n_col, ValueFilter(col.table, col.name, value, False, value)))
            i = k + n_col
            found = True
            break
        if not found:
            i += 1

    if not spans:
        return text, [], None
    seen: dict[str, ValueFilter] = {}
    for s in spans:
        prev = seen.get(s.flt.column)
        if prev is not None and prev.value.lower() != s.flt.value.lower():
            return text, [], f"several values for {s.flt.column}"  # OR-filters are left to the LLM path
        seen[s.flt.column] = s.flt
    # remove the spans (+ a directly preceding preposition / "the") from the text
    drop = [False] * len(tokens)
    for s in spans:
        for q in range(s.start, s.end):
            drop[q] = True
        p = s.start - 1
        if p >= 0 and tokens[p][0] == "the" and not drop[p]:
            drop[p] = True
            p -= 1
        if p >= 0 and tokens[p][0] in PREPOSITIONS and not drop[p]:
            drop[p] = True
    out, last = [], 0
    for (_tok, start, end), d in zip(tokens, drop, strict=True):
        if d:
            out.append(text[last:start])
            last = end
    out.append(text[last:])
    remaining = re.sub(r"\s+", " ", "".join(out)).strip()
    return remaining, list(seen.values()), None
