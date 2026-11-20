"""sqlglot helpers: parsing, local validation against the QuestionContext, LIMIT enforcement, identifier
aliasing (PII), slot templating / binding and value-free SQL shapes for conversation history.

This is NOT the security guard (apps.sqlguard is). It is a cheap local pre-check that lets nl2sql repair
model mistakes before the guard sees them and refuse obviously unsafe output early.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from nexora.contracts import GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES

MAX_LIMIT = 1000
SLOT_SENTINEL = "__nx_slot_{}__"
_SENTINEL_RE = re.compile(r"'__nx_slot_([a-z0-9_]+)__'")

# ONE vocabulary with apps.sqlguard (nexora.contracts, SECURITY_ARCHITECTURE §5.1). The local pre-check below
# only emits codes from GUARD_REJECT_CODES. REPAIRABLE codes may get ONE repair call (model mistakes); every other
# guard code is a SECURITY rejection and is terminal. Unknown codes are refused by repair() (allowlist).
REPAIRABLE_CODES = REPAIRABLE_REJECT_CODES
SECURITY_CODES = GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES
_SYSTEM_TABLE_RE = re.compile(r"^(pg_|information_schema)", re.I)
_SYSTEM_COLUMNS = frozenset({"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"})
_FORBIDDEN_NODES: tuple[type, ...] = tuple(
    t for t in (getattr(exp, n, None) for n in (
        "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Into", "Lock", "Set", "Transaction",
        "Copy", "Command", "Commit", "Rollback", "Grant", "TruncateTable", "Use", "Pragma",
    )) if t is not None
)


@dataclass(frozen=True)
class SqlIssue:
    code: str
    message: str

    @property
    def repairable(self) -> bool:
        return self.code in REPAIRABLE_CODES


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def parse_select(sql: str) -> tuple[exp.Expression | None, SqlIssue | None]:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return None, SqlIssue("parse_error", "empty SQL")
    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except (ParseError, TokenError) as exc:
        return None, SqlIssue("parse_error", str(exc).splitlines()[0][:200])
    if len(statements) != 1:
        return None, SqlIssue("multiple_statements", "exactly one statement is allowed")
    ast = statements[0]
    if isinstance(ast, exp.Command):
        return None, SqlIssue("forbidden_clause", "commands are not allowed")
    if not isinstance(ast, exp.Query):
        return None, SqlIssue("not_select", "only SELECT queries are allowed")
    for node in ast.walk():
        if _FORBIDDEN_NODES and isinstance(node, _FORBIDDEN_NODES):
            return None, SqlIssue("forbidden_clause", f"{type(node).__name__} is not allowed")
        if isinstance(node, (exp.Placeholder, exp.Parameter)):
            return None, SqlIssue("forbidden_clause", "parameters/placeholders are not allowed")
    return ast, None


def cte_names(ast: exp.Expression) -> set[str]:
    return {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}


def referenced_tables(ast: exp.Expression) -> list[exp.Table]:
    ctes = cte_names(ast)
    return [t for t in ast.find_all(exp.Table) if t.name and t.name.lower() not in ctes or (t.db or t.catalog)]


def output_aliases(ast: exp.Expression) -> set[str]:
    names: set[str] = set()
    for node in ast.find_all(exp.Alias):
        if node.alias:
            names.add(node.alias.lower())
    for node in ast.find_all(exp.TableAlias):
        for col in node.columns or []:
            names.add(col.name.lower())
    return names


def validate_against_context(ast: exp.Expression, tables: Mapping[str, object],
                             allowed_columns: Mapping[str, frozenset[str] | set[str]]) -> SqlIssue | None:
    """Tables must be logical names from the policy; columns must be allowed (or query-defined aliases)."""
    logical = {str(t).lower(): str(t) for t in tables}
    used_tables: set[str] = set()
    for t in referenced_tables(ast):
        if t.catalog or t.db:
            if _SYSTEM_TABLE_RE.match(t.db or "") or _SYSTEM_TABLE_RE.match(t.name or ""):
                return SqlIssue("system_object", f"system relation {t.sql()} is not allowed")
            return SqlIssue("table_not_allowed", f"schema-qualified table {t.sql()} is not allowed")
        if _SYSTEM_TABLE_RE.match(t.name):
            return SqlIssue("system_object", f"system relation {t.name} is not allowed")
        if t.name.lower() not in logical:
            return SqlIssue("table_not_allowed", f"unknown table {t.name}")
        used_tables.add(logical[t.name.lower()])
    allowed: set[str] = set()
    for tname in used_tables or logical.values():
        allowed |= {c.lower() for c in allowed_columns.get(tname, ())}
    aliases = output_aliases(ast)
    for col in ast.find_all(exp.Column):
        name = col.name
        if not name or isinstance(col.this, exp.Star):
            continue
        low = name.lower()
        if low in _SYSTEM_COLUMNS:
            return SqlIssue("system_object", f"system column {name} is not allowed")
        if low not in allowed and low not in aliases:
            return SqlIssue("column_not_allowed", f"unknown column {name}")
    return None


def enforce_limit(ast: exp.Expression, max_rows: int = MAX_LIMIT) -> exp.Expression:
    """Ensure the outermost query has LIMIT <= max_rows."""
    limit = ast.args.get("limit")
    current = None
    if isinstance(limit, exp.Limit):
        lit = limit.expression
        if isinstance(lit, exp.Literal) and not lit.is_string:
            try:
                current = int(float(lit.this))
            except ValueError:
                current = None
    if current is None or current > max_rows or current < 0:
        ast = ast.limit(max_rows, copy=False) if hasattr(ast, "limit") else ast
    return ast


def _int_literal(node) -> int | None:
    if isinstance(node, exp.Fetch):
        node = node.args.get("count")
    elif isinstance(node, (exp.Limit, exp.Offset)):
        node = node.expression
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(float(node.this))
        except ValueError:
            return None
    return None


def result_is_complete(sql: str | None, row_count: int, *, truncated: bool | None = None) -> bool | None:
    """Are the returned rows ALL the rows the query's grouping produces (so their sum is "the total")?

    False when the runner truncated the result, an OFFSET skipped rows, an inner query is LIMITed, or the
    outermost LIMIT n was reached (row_count >= n, e.g. "top 3 regions"). True when nothing cut the result.
    None when it cannot be told (no SQL, unparsable SQL) - callers then avoid claiming a total."""
    if truncated:
        return False
    if not sql:
        return None
    ast, issue = parse_select(sql)
    if ast is None or issue is not None:
        return None
    for node in ast.walk():
        if isinstance(node, exp.Offset) and (_int_literal(node) or 0) != 0:
            return False
        if isinstance(node, (exp.Limit, exp.Fetch)) and node.parent is not ast:
            return False  # LIMIT inside a subquery / CTE: the outer rows come from a cut set
    limit = ast.args.get("limit")
    if limit is None:
        return True
    opts = limit.args.get("limit_options") if isinstance(limit, exp.Fetch) else None
    n = _int_literal(limit)
    if n is None or (opts is not None and opts.args.get("percent")):
        return None
    return int(row_count) < n


def rename_identifiers(ast: exp.Expression, mapping: Mapping[str, str]) -> exp.Expression:
    """Rename column identifiers (case-insensitive) — used to (un)alias PII column names."""
    if not mapping:
        return ast
    low = {k.lower(): v for k, v in mapping.items()}

    def fn(node):
        if isinstance(node, exp.Column) and node.name and node.name.lower() in low:
            node.set("this", exp.to_identifier(low[node.name.lower()], quoted=True))
        return node

    return ast.transform(fn, copy=False)


def render(ast: exp.Expression) -> str:
    return ast.sql(dialect="postgres", comments=False)


def canonical(sql: str) -> str:
    ast, issue = parse_select(sql)
    if issue:
        raise ValueError(f"{issue.code}: {issue.message}")
    return render(ast)


def shape_without_literals(sql: str) -> str:
    """SQL with every literal replaced by '?' (conversation history must never carry values)."""
    ast, issue = parse_select(sql)
    if issue:
        return ""

    def fn(node):
        if isinstance(node, exp.Literal):
            return exp.Var(this="?")
        return node

    return render(ast.transform(fn))


# ----------------------------------------------------------------------------------------- slot templates
def _literal_matches(node: exp.Literal, value: object) -> bool:
    if value is None:
        return False
    if node.is_string:
        return isinstance(value, str) and node.this == value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(node.this) == float(value)
        except ValueError:
            return False
    return False


def templatize(sql: str, values: Mapping[str, object]) -> tuple[str, list[str], list[list[str]]]:
    """Replace literals equal to slot values with sentinels.

    Returns (template_sql, used_slot_names, equality_groups). An equality group lists slot names that shared
    one literal value (e.g. contiguous periods); a re-bind is valid only if they are still equal.
    """
    ast, issue = parse_select(sql)
    if issue:
        raise ValueError(f"{issue.code}: {issue.message}")
    used: list[str] = []
    groups: dict[str, list[str]] = {}

    def fn(node):
        if isinstance(node, exp.Literal):
            names = [k for k, v in values.items() if _literal_matches(node, v)]
            if names:
                primary = names[0]
                if primary not in used:
                    used.append(primary)
                if len(names) > 1:
                    groups[primary] = names
                    used.extend(n for n in names if n not in used)  # covered via the equality constraint
                return exp.Literal.string(SLOT_SENTINEL.format(primary))
        return node

    template = render(ast.transform(fn))
    return template, used, list(groups.values())


def _sql_literal(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("unsupported slot value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def bind(template: str, values: Mapping[str, object], equalities: list[list[str]] | None = None) -> str | None:
    """Fill sentinels with new slot values. Returns None when the template cannot be bound safely."""
    for group in equalities or []:
        vals = {repr(values.get(n)) for n in group}
        if len(vals) != 1:
            return None
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if values.get(name) is None:
            missing.append(name)
            return m.group(0)
        return _sql_literal(values[name])

    out = _SENTINEL_RE.sub(sub, template)
    if missing:
        return None
    ast, issue = parse_select(out)
    if issue:
        return None
    return render(ast)


def template_slots(template: str) -> set[str]:
    return set(_SENTINEL_RE.findall(template))
