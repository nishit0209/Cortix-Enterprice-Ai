"""Secure-source substitution and SQL regeneration (SECURITY_ARCHITECTURE.md §5.1 steps 10-12).

Every base-table reference, in every scope (CTEs, subqueries, set-operation branches, LATERAL), becomes

    (SELECT <allowed columns, masked at the source> FROM "<schema>"."<table>" AS "_src" WHERE <row predicates>)
        AS "<original alias>"

so a denied column does not exist anywhere in the query, a masked column is masked for WHERE / GROUP BY / JOIN /
ORDER BY too, and the row predicate cannot be bypassed by OR-ing it away. Predicate values and every user string
literal are bound parameters (`%(pN)s`), never concatenated text. The result is wrapped in
`SELECT * FROM (<query>) AS "_q" LIMIT <row_cap + 1>` so the executor can detect truncation.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import hmac
import re
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from nexora.contracts import MaskSpec, QueryPolicy

from .dialect import DIALECT
from .exceptions import GuardReject

SRC_ALIAS = "_src"
WRAP_ALIAS = "_q"
PLACEHOLDER_RE = re.compile(r"%\((p\d+)\)s")
_PHYSICAL_PART_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_GENERATED_ALIAS_RE = re.compile(r"^_col_\d+$")
_FUNC_HEAD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")
MAX_KEEP_LAST = 64

# Mask templates, parsed once with the guard dialect so the regenerated SQL is a round-trip fixed point.
# `x` is replaced by the source column, `%(salt)s` / 4 by bound values.
_MASK_NULL = "CASE WHEN FALSE THEN x END"
_MASK_HASH = "ENCODE(SHA256(CONVERT_TO(CAST(x AS TEXT) || %(salt)s, 'UTF8')), 'hex')"  # SECURITY_ARCHITECTURE §5
_MASK_PARTIAL = "REPEAT('*', GREATEST(LENGTH(CAST(x AS TEXT)) - 4, 0)) || RIGHT(CAST(x AS TEXT), 4)"


def _template(sql: str) -> exp.Expression:
    return sqlglot.parse_one(sql, read=DIALECT)


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _src_column(name: str) -> exp.Column:
    if "%" in name or "\x00" in name or not name:
        raise GuardReject("invalid_sql", "The dataset has a column name the guard cannot express.")
    return exp.Column(this=_ident(name), table=_ident(SRC_ALIAS))


def split_physical(physical: str) -> tuple[str, str]:
    parts = str(physical).split(".")
    if len(parts) != 2 or not all(_PHYSICAL_PART_RE.match(p) for p in parts):
        raise GuardReject("invalid_sql", "The dataset's warehouse table name is invalid.")
    return parts[0], parts[1]


def mask_salt(org_id: Any) -> str:
    """Per-org salt for mask_hash (deterministic so joins on hashed values still work inside one org)."""
    from django.conf import settings

    key = str(getattr(settings, "SECRET_KEY", "") or "nexora").encode()
    return hmac.new(key, f"sqlguard-mask-hash:{org_id}".encode(), hashlib.sha256).hexdigest()[:32]


class ParamRegistry:
    """Bound parameters: identical values share one name (so GROUP BY expressions keep matching the SELECT)."""

    def __init__(self) -> None:
        self._names: dict[tuple[str, str], str] = {}
        self.values: dict[str, Any] = {}

    def add(self, value: Any) -> exp.Placeholder:
        value = _adapt(value)
        key = (type(value).__name__, repr(value))
        name = self._names.get(key)
        if name is None:
            name = f"p{len(self._names) + 1}"
            self._names[key] = name
            self.values[name] = value
        return exp.Placeholder(this=exp.to_identifier(name))


def _adapt(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float, decimal.Decimal, dt.date, dt.datetime, dt.time)):
        return value
    if isinstance(value, UUID):
        return str(value)
    return str(value)


class Rewriter:
    def __init__(self, policy: QueryPolicy):
        self.policy = policy
        self.params = ParamRegistry()

    # ------------------------------------------------------------------------------------------ public
    def rewrite(self, root: exp.Expression) -> tuple[str, dict[str, Any]]:
        self._name_outputs(root)
        self._bind_literals(root)
        self._substitute(root)
        row_cap = max(0, int(self.policy.row_cap))
        wrapped = (
            exp.Select(expressions=[exp.Star()])
            .from_(exp.Subquery(this=root, alias=exp.TableAlias(this=_ident(WRAP_ALIAS))), copy=False)
            .limit(row_cap + 1, copy=False)
        )
        sql = wrapped.sql(dialect=DIALECT, comments=False)
        if "%" in PLACEHOLDER_RE.sub("", sql):
            raise GuardReject("invalid_sql", "A '%' can only appear inside string literals.")
        self._roundtrip(sql)
        names = set(PLACEHOLDER_RE.findall(sql))
        params = {k: v for k, v in self.params.values.items() if k in names}
        if names != set(params):
            raise GuardReject("invalid_sql", "Internal parameter mismatch.")
        return sql, params

    # ------------------------------------------------------------------------------------------ steps
    def _name_outputs(self, root: exp.Expression) -> None:
        """qualify() names unnamed outputs `_col_N`; give them PostgreSQL-like names instead (sum, count, ...)."""
        select = root
        while isinstance(select, exp.SetOperation):
            select = select.this
        if not isinstance(select, exp.Select):
            return
        names = [e.alias_or_name for e in select.expressions]
        for idx, proj in enumerate(select.expressions):
            if not isinstance(proj, exp.Alias) or not _GENERATED_ALIAS_RE.match(proj.alias):
                continue
            new = _pg_output_name(proj.this)
            if new in names:
                continue
            old = proj.alias
            proj.set("alias", _ident(new))
            names[idx] = new
            order = root.args.get("order")
            if order is not None:
                for col in order.find_all(exp.Column):
                    if not col.table and col.name == old:
                        col.set("this", _ident(new))

    def _bind_literals(self, root: exp.Expression) -> None:
        for lit in list(root.find_all(exp.Literal)):
            if not lit.is_string:
                continue
            if lit.arg_key in ("format", "unit") or lit.find_ancestor(exp.Interval, exp.DataType) is not None:
                continue  # stays inline (format masks / interval text); rewrite() refuses any stray '%' later
            lit.replace(self.params.add(str(lit.this)))

    def _substitute(self, root: exp.Expression) -> None:
        targets: list[tuple[exp.Table, str]] = []
        for scope in traverse_scope(root):
            for table in scope.tables:
                if table.name in scope.cte_sources and not table.db:
                    continue
                targets.append((table, table.name))
        for table, logical in targets:
            if logical not in self.policy.tables:  # validated earlier; fail closed regardless
                raise GuardReject("table_not_allowed", f"Table {logical!r} is not available in this dataset.")
            alias = table.alias or logical
            secure = self.secure_source(logical)
            table.replace(exp.Subquery(this=secure, alias=exp.TableAlias(this=_ident(alias))))

    # ------------------------------------------------------------------------------------------ builders
    def secure_source(self, logical: str) -> exp.Select:
        schema, table = split_physical(self.policy.tables[logical])
        projections: list[exp.Expression] = []
        for col in sorted(self.policy.allowed_columns.get(logical, frozenset())):
            ref = _src_column(col)
            mask = self.policy.column_masks.get((logical, col))
            expr = self.mask_expression(ref, mask) if mask is not None else ref
            projections.append(exp.Alias(this=expr, alias=_ident(col)))
        if not projections:
            projections.append(exp.Alias(this=exp.Literal.number(1), alias=_ident("_row")))
        source = exp.Table(this=_ident(table), db=_ident(schema), alias=exp.TableAlias(this=_ident(SRC_ALIAS)))
        select = exp.Select(expressions=projections).from_(source, copy=False)
        predicate = self.row_predicate(logical)
        if predicate is not None:
            select = select.where(predicate, copy=False)
        return select

    def mask_expression(self, ref: exp.Column, mask: MaskSpec) -> exp.Expression:
        if mask.kind == "mask_null":
            node = _template(_MASK_NULL)
        elif mask.kind == "mask_hash":
            node = _template(_MASK_HASH)
            for ph in list(node.find_all(exp.Placeholder)):
                ph.replace(self.params.add(mask_salt(self.policy.org_id)))
        elif mask.kind == "mask_partial":
            keep = max(0, min(int(mask.keep_last), MAX_KEEP_LAST))
            node = _template(_MASK_PARTIAL)
            for lit in list(node.find_all(exp.Literal)):
                if not lit.is_string and lit.this == "4":
                    lit.replace(exp.Literal.number(keep))
        else:  # unknown mask: fail closed by hiding the value
            node = _template(_MASK_NULL)
        for col in list(node.find_all(exp.Column)):
            if col.name == "x":
                col.replace(ref.copy())
        return node

    def row_predicate(self, logical: str) -> exp.Expression | None:
        pred = self.policy.row_predicates.get(logical)
        if pred is None or not pred.conditions:
            return None
        parts = [self._condition(col, op, value) for col, op, value in pred.conditions]
        return exp.and_(*parts, copy=False) if len(parts) > 1 else parts[0]

    def _condition(self, column: str, op: str, value: Any) -> exp.Expression:
        ref = _src_column(str(column))
        compare = {"=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}
        if op in compare:
            if value is None:
                return exp.false()
            return compare[op](this=ref, expression=self.params.add(value))
        if op in ("in", "not_in"):
            values = tuple(value) if isinstance(value, (list, tuple, set, frozenset)) else (value,)
            values = tuple(v for v in values if v is not None) if op == "in" else values
            if not values:
                return exp.false() if op == "in" else exp.true()
            if op == "not_in" and any(v is None for v in values):
                return exp.false()  # x NOT IN (..., NULL) is never true
            node = exp.In(this=ref, expressions=[self.params.add(v) for v in values])
            return node if op == "in" else exp.Not(this=node)
        if op == "is_null":
            return exp.Is(this=ref, expression=exp.Null())
        if op == "not_null":
            return exp.Not(this=exp.Is(this=ref, expression=exp.Null()))
        raise GuardReject("invalid_sql", f"Unsupported row-policy operator {op!r}.")

    # ------------------------------------------------------------------------------------------ checks
    @staticmethod
    def _roundtrip(sql: str) -> None:
        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except SqlglotError as exc:
            raise GuardReject("invalid_sql", "The rewritten query could not be re-parsed.") from exc
        if len(statements) != 1:
            raise GuardReject("invalid_sql", "The rewritten query is not a single statement.")
        if statements[0].sql(dialect=DIALECT, comments=False) != sql:
            raise GuardReject("invalid_sql", "The rewritten query does not round-trip.")


def _pg_output_name(node: exp.Expression) -> str:
    """Approximates PostgreSQL's default output column name for an unnamed expression."""
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Column):
        return node.this.name
    if isinstance(node, exp.Func):
        m = _FUNC_HEAD_RE.match(node.sql(dialect=DIALECT))
        if m:
            return m.group(1).lower()
    if isinstance(node, exp.Case):
        return "case"
    return "?column?"
