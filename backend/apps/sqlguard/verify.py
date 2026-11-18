"""Independent post-condition check of guard output (defence in depth; also re-run by execute()).

Re-parses the executed SQL and proves, without trusting the rewriter, that:
  * it is exactly one SELECT with no DML/DDL/utility node and no positional parameters;
  * every physical relation is one of the policy's tables and appears only as the single source of a secure
    sub-select aliased "_src" (all other table references are CTE names);
  * each secure sub-select reads only allowed columns (plus row-predicate columns in its WHERE), outputs only
    allowed column names, and carries a WHERE when the policy has a row predicate for that table;
  * the bound parameters are exactly the placeholders used.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from nexora.contracts import QueryPolicy

from . import allowlist as al
from .dialect import DIALECT
from .exceptions import GuardReject
from .rewrite import PLACEHOLDER_RE, SRC_ALIAS


def _fail(reason: str) -> GuardReject:
    return GuardReject("invalid_sql", f"Guard invariant violated: {reason}.")


def verify_output(sql: str, params: Mapping[str, Any], policy: QueryPolicy) -> None:
    try:
        statements = [s for s in sqlglot.parse(sql or "", read=DIALECT) if s is not None]
    except SqlglotError as exc:
        raise _fail("output does not parse") from exc
    if len(statements) != 1 or not isinstance(statements[0], al.ROOT_TYPES):
        raise _fail("output is not a single SELECT")
    tree = statements[0]
    for node in tree.walk():
        if isinstance(node, tuple(al.NOT_SELECT_NODES)) or isinstance(node, (exp.Parameter, exp.Lock)):
            raise _fail(f"forbidden node {type(node).__name__}")
    used = {p.name for p in tree.find_all(exp.Placeholder)}
    if used != set(PLACEHOLDER_RE.findall(sql)) or used != set(params):
        raise _fail("bound parameters do not match the placeholders")

    physical = {}
    for logical, phys in policy.tables.items():
        physical[str(phys).lower()] = logical
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if table.args.get("catalog"):
            raise _fail("database-qualified relation")
        key = f"{table.db}.{table.name}".lower()
        if not table.db:
            if table.name in ctes:
                continue
            raise _fail(f"unresolved relation {table.name!r}")
        logical = physical.get(key)
        if logical is None:
            raise _fail(f"relation {key} is outside the policy")
        _check_secure_select(table, logical, policy)


def _check_secure_select(table: exp.Table, logical: str, policy: QueryPolicy) -> None:
    if table.alias != SRC_ALIAS:
        raise _fail("physical relation without the secure alias")
    from_ = table.parent
    select = from_.parent if isinstance(from_, exp.From) else None
    if not isinstance(select, exp.Select) or select.args.get("joins") or not isinstance(select.parent, exp.Subquery):
        raise _fail("physical relation outside a secure sub-select")
    for key in ("group", "having", "order", "limit", "offset", "with_", "with", "distinct", "windows"):
        if select.args.get(key):
            raise _fail("secure sub-select carries extra clauses")
    allowed = set(policy.allowed_columns.get(logical, frozenset()))
    pred = policy.row_predicates.get(logical)
    pred_cols = {str(c) for c, _, _ in (pred.conditions if pred else ())}
    for proj in select.expressions:
        name = proj.alias_or_name
        if name not in allowed and name != "_row":
            raise _fail(f"secure sub-select outputs {name!r}")
        for col in proj.find_all(exp.Column):
            if col.name not in allowed:
                raise _fail(f"secure sub-select reads {col.name!r}")
    where = select.args.get("where")
    if pred_cols:
        if where is None:
            raise _fail("row predicate missing")
        where_cols = {c.name for c in where.find_all(exp.Column)}
        if not _expected_columns(pred.conditions) <= where_cols:
            raise _fail("row predicate incomplete")
    if where is not None:
        for col in where.find_all(exp.Column):
            if col.name not in pred_cols:
                raise _fail(f"secure predicate reads {col.name!r}")
        if where.find(exp.Select) is not None:
            raise _fail("secure predicate contains a subquery")


def _expected_columns(conditions) -> set[str]:
    """Columns that must appear in the secure WHERE (conditions that fold to TRUE/FALSE carry no column)."""
    out = set()
    for col, op, value in conditions:
        if op in ("in", "not_in"):
            values = tuple(value) if isinstance(value, (list, tuple, set, frozenset)) else (value,)
            if op == "in" and not [v for v in values if v is not None]:
                continue
            if op == "not_in" and (not values or any(v is None for v in values)):
                continue
        elif op in ("=", "!=", "<", "<=", ">", ">=") and value is None:
            continue
        out.add(str(col))
    return out
