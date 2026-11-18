"""SQL guard validation pipeline (SECURITY_ARCHITECTURE.md §5.1). Pure: no database, no Django models.

    validate(sql, policy) -> GuardResult

Invariant: no SQL reaches a database unless it was parsed, validated, rewritten and regenerated here. Raw input
is never executed: the executed text is generated from the rewritten AST, string literals are bound parameters
and comments are dropped, so lexer differences between sqlglot and PostgreSQL cannot smuggle anything through.

Steps (each rejection uses a code from nexora.contracts.GUARD_REJECT_CODES):
   1. size: <= 10 KB text, <= 1,500 AST nodes, nesting depth <= 12                        too_large
   2. tokenize + parse (postgres); exactly one statement                     parse_error, multiple_statements
   3. root is SELECT / set operation; no DML/DDL/utility node anywhere (SELECT INTO, data-modifying CTE)  not_select
   4. no FOR UPDATE/SHARE, placeholders/parameters, TABLESAMPLE, dollar/unicode/escape strings   forbidden_clause
      no WITH RECURSIVE                                                                      recursive_cte
   5. no system columns (ctid, xmin, ...), no NATURAL JOIN                                   column_not_allowed
   6. function allowlist (typed sqlglot functions + anonymous names), no schema-qualified calls  forbidden_function
   7. casts only to basic types (no reg*, no user-defined)                                  forbidden_cast
   8. no functions / VALUES-less table functions in FROM or LATERAL                         set_returning_function
   9. tables: no catalog/schema-qualified names, no system objects, CTEs cannot shadow policy tables, every
      base table (resolved per scope) is a logical table of the policy       table_not_allowed, system_object
  10. columns: sqlglot qualify against a schema of ONLY the allowed columns (stars expand to allowed columns);
      whole-row references rejected                    column_not_allowed, ambiguous_column, unknown_table
  11. rewrite (apps.sqlguard.rewrite): secure-source substitution in every scope, masks, row predicates with
      bound parameters, string literals bound, wrap with LIMIT row_cap+1
  12. regenerate without comments; round-trip fixed point; independent post-condition check     invalid_sql
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from nexora.contracts import GuardResult, QueryPolicy

from . import allowlist as al
from .dialect import DIALECT
from .exceptions import GuardReject
from .rewrite import Rewriter
from .verify import verify_output

log = logging.getLogger("nexora.sqlguard")

MAX_SQL_BYTES = 10 * 1024
MAX_AST_NODES = 1500
MAX_NESTING = 12
MAX_PAREN_DEPTH = 64

_EXOTIC_STRING_TOKENS = frozenset(
    t for t in (
        getattr(TokenType, name, None)
        for name in ("HEREDOC_STRING", "UNICODE_STRING", "BYTE_STRING", "RAW_STRING", "NATIONAL_STRING",
                     "BIT_STRING", "HEX_STRING")
    )
    if t is not None
)
_UNRESOLVED_COL_RE = re.compile(r"Column '([^']*)' could not be resolved")
_UNRESOLVED_FOR_TABLE_RE = re.compile(r"could not be resolved for table: '([^']*)'")
_UNKNOWN_COLUMN_RE = re.compile(r"Unknown column: ([^\s.]+)")
_TABLE_COLUMN = getattr(exp, "TableColumn", None)  # sqlglot's node for a whole-row reference (SELECT t FROM x t)


def _reject(code: str, message: str) -> GuardReject:
    return GuardReject(code, message)


def rejected(code: str, message: str) -> GuardResult:
    return GuardResult(ok=False, reject_code=code, message=message, executed_sql=None, params={}, tables=())


# ------------------------------------------------------------------------------------------------ entry point
def validate(sql: str, policy: QueryPolicy) -> GuardResult:
    """Validate and rewrite `sql` for `policy`. Never raises; never returns SQL for a rejected input."""
    try:
        return _validate(sql, policy)
    except GuardReject as rej:
        return rejected(rej.code, rej.message)
    except RecursionError:
        return rejected("too_large", "The query is nested too deeply.")
    except Exception:  # noqa: BLE001 - fail closed on anything unexpected
        log.exception("sqlguard: unexpected error while validating")
        return rejected("invalid_sql", "The query could not be validated.")


def _validate(sql: str, policy: QueryPolicy) -> GuardResult:
    root = parse_single(sql)
    check_static(root)
    tables = check_tables(root, policy)
    qualified = check_columns(root, policy)
    rewriter = Rewriter(policy)
    executed_sql, params = rewriter.rewrite(qualified)
    verify_output(executed_sql, params, policy)
    return GuardResult(ok=True, reject_code=None, message="ok", executed_sql=executed_sql, params=params,
                       tables=tuple(sorted(tables)))


# ------------------------------------------------------------------------------------------------ 1-2 parsing
def _paren_depth(sql: str) -> int:
    depth = best = 0
    for ch in sql:
        if ch == "(":
            depth += 1
            best = max(best, depth)
        elif ch == ")":
            depth = max(0, depth - 1)
    return best


def parse_single(sql: str) -> exp.Expression:
    if not isinstance(sql, str):
        raise _reject("parse_error", "The query must be text.")
    if "\x00" in sql:
        raise _reject("parse_error", "The query contains a NUL character.")
    if len(sql.encode("utf-8", "surrogatepass")) > MAX_SQL_BYTES:
        raise _reject("too_large", f"The query is longer than {MAX_SQL_BYTES} bytes.")
    if not sql.strip():
        raise _reject("parse_error", "The query is empty.")
    if _paren_depth(sql) > MAX_PAREN_DEPTH:
        raise _reject("too_large", "The query is nested too deeply.")
    try:
        tokens = DIALECT().tokenizer_class().tokenize(sql)
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (TokenError, ParseError) as exc:
        raise _reject("parse_error", f"The query could not be parsed: {_short(exc)}") from exc
    except SqlglotError as exc:
        raise _reject("parse_error", f"The query could not be parsed: {_short(exc)}") from exc
    if not statements:
        raise _reject("parse_error", "The query is empty.")
    if len(statements) > 1:
        raise _reject("multiple_statements", "Only one statement is allowed.")
    if any(t.token_type in _EXOTIC_STRING_TOKENS for t in tokens):
        raise _reject("parse_error", "Dollar-quoted, escape (E''), unicode (U&'') and bit strings are not supported; "
                                     "use plain single-quoted strings.")
    root = statements[0]
    while isinstance(root, exp.Subquery) and not root.alias and isinstance(root.this, al.ROOT_TYPES):
        root = root.this  # "(SELECT ...)" at the top level
    if root.parent is not None:
        root.pop()
    nodes = 0
    for _ in root.walk():
        nodes += 1
        if nodes > MAX_AST_NODES:
            raise _reject("too_large", f"The query has more than {MAX_AST_NODES} syntax nodes.")
    if _nesting(root) > MAX_NESTING:
        raise _reject("too_large", f"The query nests more than {MAX_NESTING} subqueries.")
    return root


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__


def _nesting(root: exp.Expression) -> int:
    best = 0
    for node in root.find_all(exp.Select):
        depth, parent = 1, node.parent
        while parent is not None:
            if isinstance(parent, exp.Select):
                depth += 1
            parent = parent.parent
        best = max(best, depth)
    return best


# ------------------------------------------------------------------------------------------------ 3-8 static
def check_static(root: exp.Expression) -> None:
    if not isinstance(root, al.ROOT_TYPES):
        raise _reject("not_select", "Only SELECT queries are allowed.")
    nodes = list(root.walk())
    for node in nodes:
        if isinstance(node, exp.Select) and not node.expressions:
            raise _reject("parse_error", "SELECT needs at least one output column.")
    for node in nodes:
        if isinstance(node, tuple(al.NOT_SELECT_NODES)) or (isinstance(node, exp.Select) and node.args.get("into")):
            raise _reject("not_select", f"{type(node).__name__} is not allowed; only plain SELECT queries are.")
    for node in nodes:
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise _reject("recursive_cte", "WITH RECURSIVE is not allowed.")
    for node in nodes:
        if isinstance(node, tuple(al.FORBIDDEN_CLAUSE_NODES)):
            raise _reject("forbidden_clause", f"{_node_label(node)} is not allowed.")
        if isinstance(node, exp.Select) and node.args.get("locks"):
            raise _reject("forbidden_clause", "FOR UPDATE / FOR SHARE is not allowed.")
        if isinstance(node, exp.Table):
            extra = {k for k, v in node.args.items() if v and k not in ("this", "db", "catalog", "alias")}
            if extra:
                raise _reject("forbidden_clause", f"Table modifiers are not allowed ({', '.join(sorted(extra))}).")
        if isinstance(node, exp.Identifier) and ("%" in node.name or "\x00" in node.name):
            raise _reject("forbidden_clause", "Identifiers may not contain '%'.")
    for node in nodes:
        if isinstance(node, exp.Column) and node.name.lower() in al.SYSTEM_COLUMNS:
            raise _reject("column_not_allowed", f"System column {node.name!r} is not allowed.")
        if isinstance(node, exp.Join) and str(node.args.get("method") or "").upper() == "NATURAL":
            raise _reject("column_not_allowed", "NATURAL JOIN is not allowed; join on explicit columns.")
    for node in nodes:
        _check_function(node)
    for node in nodes:
        if isinstance(node, exp.DataType) and node.this not in al.ALLOWED_CAST_TYPES:
            raise _reject("forbidden_cast", f"Casting to {node.sql(dialect=DIALECT)} is not allowed.")
    for node in nodes:
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise _reject("set_returning_function", "Functions are not allowed in FROM.")
        if isinstance(node, exp.Lateral) and not isinstance(node.this, exp.Subquery):
            raise _reject("set_returning_function", "LATERAL is only allowed with a subquery.")


def _node_label(node: exp.Expression) -> str:
    if isinstance(node, (exp.Placeholder, exp.Parameter)):
        return "A query parameter/placeholder"
    if isinstance(node, exp.Lock):
        return "FOR UPDATE / FOR SHARE"
    return type(node).__name__


def _check_function(node: exp.Expression) -> None:
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
        raise _reject("forbidden_function", "Schema-qualified function calls are not allowed.")
    if not isinstance(node, exp.Func):
        return
    if isinstance(node, exp.Anonymous):
        name = node.name if isinstance(node.name, str) else str(node.this)
        quoted = isinstance(node.this, exp.Identifier) and node.this.quoted
        lowered = name.lower()
        if quoted and name != lowered:
            raise _reject("forbidden_function", f"Function {name!r} is not allowed.")
        if lowered.startswith(al.FORBIDDEN_FUNCTION_PREFIXES) or lowered not in al.ALLOWED_FUNCTION_NAMES:
            raise _reject("forbidden_function", f"Function {lowered}() is not allowed.")
        return
    if type(node) not in al.ALLOWED_FUNCTION_CLASSES or type(node) in al.SET_RETURNING_CLASSES:
        label = _func_label(node)
        raise _reject("forbidden_function", f"Function {label}() is not allowed.")


def _func_label(node: exp.Func) -> str:
    try:
        head = node.sql(dialect=DIALECT).split("(", 1)[0].strip()
    except SqlglotError:
        head = ""
    return head.lower() if head and len(head) < 64 else type(node).__name__.lower()


# ------------------------------------------------------------------------------------------------ 9 tables
def _cte_names(root: exp.Expression) -> set[str]:
    return {cte.alias_or_name for cte in root.find_all(exp.CTE)}


def check_tables(root: exp.Expression, policy: QueryPolicy) -> set[str]:
    """Normalize identifiers in place and return the logical policy tables the query reads."""
    normalize_identifiers(root, dialect=DIALECT)
    allowed_tables = set(policy.tables)
    ctes = _cte_names(root)
    for name in ctes:
        if al.is_system_name(name):
            raise _reject("system_object", f"CTE name {name!r} is reserved.")
        if name in allowed_tables:
            raise _reject("table_not_allowed", f"CTE {name!r} would shadow a dataset table.")
    for table in root.find_all(exp.Table):
        if table.args.get("catalog"):
            raise _reject("table_not_allowed", "Database-qualified table names are not allowed.")
        if table.db:
            if al.is_system_name(table.db):
                raise _reject("system_object", "System catalogs are not accessible.")
            raise _reject("table_not_allowed", "Schema-qualified table names are not allowed; use the dataset table name.")
        if al.is_system_name(table.name) and table.name not in ctes:
            raise _reject("system_object", "System catalogs are not accessible.")

    used: set[str] = set()
    seen: set[int] = set()
    try:
        scopes = list(traverse_scope(root))
    except OptimizeError as exc:
        raise _reject("invalid_sql", f"The query structure is not supported: {_short(exc)}") from exc
    for scope in scopes:
        for table in scope.tables:
            seen.add(id(table))
            if table.name in scope.cte_sources:
                continue
            if table.name not in allowed_tables:
                raise _reject("table_not_allowed", f"Table {table.name!r} is not available in this dataset.")
            used.add(table.name)
    for table in root.find_all(exp.Table):  # tables outside every scope are treated as base tables
        if id(table) not in seen:
            if table.name not in allowed_tables:
                raise _reject("table_not_allowed", f"Table {table.name!r} is not available in this dataset.")
            used.add(table.name)
    return used


# ------------------------------------------------------------------------------------------------ 10 columns
def _schema(policy: QueryPolicy) -> dict[str, dict[str, str]]:
    """Only allowed columns exist. A table with none exposes the constant "_row" (its secure source is
    `SELECT 1 AS "_row"`), so COUNT(*) still works while every real column is unresolvable."""
    out = {}
    for t in policy.tables:
        cols = sorted(policy.allowed_columns.get(t, frozenset()))
        out[t] = {c: "text" for c in cols} if cols else {"_row": "int"}
    return out


def _map_qualify_error(exc: Exception, policy: QueryPolicy, used: Iterable[str]) -> GuardReject:
    message = str(exc)
    if "Ambiguous" in message or "ambiguous" in message:
        return _reject("ambiguous_column", _short(exc))
    m = _UNRESOLVED_FOR_TABLE_RE.search(message)
    if m:
        return _reject("unknown_table", f"Unknown table or alias {m.group(1)!r}.")
    m = _UNRESOLVED_COL_RE.search(message) or _UNKNOWN_COLUMN_RE.search(message)
    if m:
        name = m.group(1)
        owners = [t for t in used if name in policy.allowed_columns.get(t, frozenset())]
        if len(owners) > 1:
            return _reject("ambiguous_column", f"Column {name!r} is ambiguous; qualify it with a table alias.")
        return _reject("column_not_allowed", f"Column {name!r} is not available.")
    if "Cannot automatically join" in message or "join" in message.lower():
        return _reject("column_not_allowed", "The join column is not available.")
    if "star" in message.lower() or "*" in message:
        return _reject("column_not_allowed", "The selected columns could not be resolved.")
    return _reject("invalid_sql", f"The query could not be resolved: {_short(exc)}")


def check_columns(root: exp.Expression, policy: QueryPolicy) -> exp.Expression:
    used = [t.name for t in root.find_all(exp.Table)]
    try:
        qualified = qualify(
            root,
            schema=_schema(policy),
            dialect=DIALECT,
            expand_stars=True,
            validate_qualify_columns=True,
            quote_identifiers=True,
        )
    except OptimizeError as exc:
        raise _map_qualify_error(exc, policy, used) from exc
    except SqlglotError as exc:
        raise _reject("invalid_sql", f"The query could not be resolved: {_short(exc)}") from exc
    _check_unqualified(qualified)
    return qualified


def _projection_names(node: exp.Expression) -> set[str]:
    while isinstance(node, exp.SetOperation):
        node = node.this
    if isinstance(node, exp.Subquery):
        node = node.this
    if isinstance(node, exp.Select):
        return {e.alias_or_name for e in node.expressions}
    return set()


def _check_unqualified(root: exp.Expression) -> None:
    """After qualify every column carries its source; the only unqualified ones allowed are ORDER BY references
    to an output alias. Anything else is a whole-row reference (`SELECT t FROM x t`) or unresolvable."""
    if _TABLE_COLUMN is not None and root.find(_TABLE_COLUMN) is not None:
        raise _reject("column_not_allowed", "Whole-row references (SELECT t FROM x t) are not allowed.")
    for col in root.find_all(exp.Column):
        if col.table or isinstance(col.this, exp.Star):
            continue
        order = col.find_ancestor(exp.Order)
        owner = order.parent if order is not None else None
        if owner is not None and col.name in _projection_names(owner):
            # the column must not sit inside a nested query of the ORDER BY
            if col.find_ancestor(exp.Select, exp.SetOperation) is owner:
                continue
        raise _reject("column_not_allowed", f"{col.name!r} is not a column you can select (whole-row references "
                                            "are not allowed).")
