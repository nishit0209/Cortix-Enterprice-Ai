"""Safe boolean expressions -> Polars expressions (no user code is ever executed).

A small SQL subset parsed with sqlglot: column references, literals, comparison, AND/OR/NOT, arithmetic,
IN, BETWEEN, IS [NOT] NULL, [I]LIKE, and LOWER/UPPER/LENGTH/TRIM/ABS/COALESCE. Anything else is rejected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import polars as pl
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

MAX_EXPRESSION_LENGTH = 1000
MAX_NODES = 200


class ExpressionError(ValueError):
    pass


def _like_to_regex(pattern: str) -> str:
    out = ["^"]
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    out.append("$")
    return "".join(out)


def _literal(node: exp.Literal) -> pl.Expr:
    if node.is_string:
        return pl.lit(node.this, dtype=pl.String)
    text = node.this
    try:
        return pl.lit(int(text)) if re.fullmatch(r"[-+]?\d+", text) else pl.lit(float(text))
    except ValueError as exc:
        raise ExpressionError(f"invalid number {text!r}") from exc


class _Compiler:
    def __init__(self, columns: Iterable[str]):
        self.columns = set(columns)
        self.used: set[str] = set()

    def compile(self, node: exp.Expression) -> pl.Expr:
        method = getattr(self, f"_c_{type(node).__name__}", None)
        if method is None:
            raise ExpressionError(f"'{node.sql(dialect='postgres')[:60]}' is not allowed in quality expressions")
        return method(node)

    def _bin(self, node, op):
        return op(self.compile(node.this), self.compile(node.expression))

    # leaves
    def _c_Column(self, node: exp.Column) -> pl.Expr:
        if node.table:
            raise ExpressionError("qualified column references are not allowed")
        name = node.name
        if name not in self.columns:
            raise ExpressionError(f"unknown column '{name}'")
        self.used.add(name)
        return pl.col(name)

    def _c_Literal(self, node):
        return _literal(node)

    def _c_Boolean(self, node):
        return pl.lit(bool(node.this))

    def _c_Null(self, node):
        return pl.lit(None)

    def _c_Paren(self, node):
        return self.compile(node.this)

    # logic
    def _c_And(self, node):
        return self._bin(node, lambda a, b: a & b)

    def _c_Or(self, node):
        return self._bin(node, lambda a, b: a | b)

    def _c_Not(self, node):
        return ~self.compile(node.this)

    # comparison
    def _c_EQ(self, node):
        return self._bin(node, lambda a, b: a == b)

    def _c_NEQ(self, node):
        return self._bin(node, lambda a, b: a != b)

    def _c_GT(self, node):
        return self._bin(node, lambda a, b: a > b)

    def _c_GTE(self, node):
        return self._bin(node, lambda a, b: a >= b)

    def _c_LT(self, node):
        return self._bin(node, lambda a, b: a < b)

    def _c_LTE(self, node):
        return self._bin(node, lambda a, b: a <= b)

    def _c_Is(self, node):
        if not isinstance(node.expression, exp.Null):
            raise ExpressionError("only IS [NOT] NULL is supported")
        inner = self.compile(node.this)
        return inner.is_not_null() if node.args.get("negate") else inner.is_null()

    def _c_In(self, node):
        values = []
        for v in node.expressions:
            if isinstance(v, exp.Literal):
                values.append(v.this if v.is_string else (int(v.this) if re.fullmatch(r"[-+]?\d+", v.this) else float(v.this)))
            elif isinstance(v, exp.Boolean):
                values.append(bool(v.this))
            else:
                raise ExpressionError("IN lists may only contain literals")
        if node.args.get("query") is not None:
            raise ExpressionError("subqueries are not allowed")
        result = self.compile(node.this).is_in(values)
        return ~result if node.args.get("negate") else result

    def _c_Between(self, node):
        inner = self.compile(node.this)
        return (inner >= self.compile(node.args["low"])) & (inner <= self.compile(node.args["high"]))

    def _c_Like(self, node):
        pat = node.expression
        if not isinstance(pat, exp.Literal) or not pat.is_string:
            raise ExpressionError("LIKE needs a string literal pattern")
        return self.compile(node.this).cast(pl.String).str.contains(_like_to_regex(pat.this))

    def _c_ILike(self, node):
        pat = node.expression
        if not isinstance(pat, exp.Literal) or not pat.is_string:
            raise ExpressionError("ILIKE needs a string literal pattern")
        return self.compile(node.this).cast(pl.String).str.contains("(?i)" + _like_to_regex(pat.this))

    # arithmetic
    def _c_Add(self, node):
        return self._bin(node, lambda a, b: a + b)

    def _c_Sub(self, node):
        return self._bin(node, lambda a, b: a - b)

    def _c_Mul(self, node):
        return self._bin(node, lambda a, b: a * b)

    def _c_Div(self, node):
        return self._bin(node, lambda a, b: a / b)

    def _c_Mod(self, node):
        return self._bin(node, lambda a, b: a % b)

    def _c_Neg(self, node):
        return -self.compile(node.this)

    # functions
    def _c_Lower(self, node):
        return self.compile(node.this).cast(pl.String).str.to_lowercase()

    def _c_Upper(self, node):
        return self.compile(node.this).cast(pl.String).str.to_uppercase()

    def _c_Length(self, node):
        return self.compile(node.this).cast(pl.String).str.len_chars()

    def _c_Trim(self, node):
        if node.args.get("expression") is not None:
            raise ExpressionError("TRIM(... FROM ...) is not supported")
        return self.compile(node.this).cast(pl.String).str.strip_chars()

    def _c_Abs(self, node):
        return self.compile(node.this).abs()

    def _c_Coalesce(self, node):
        return pl.coalesce([self.compile(node.this), *[self.compile(e) for e in node.expressions]])


def compile_expression(text: str, columns: Iterable[str]) -> tuple[pl.Expr, set[str]]:
    """Compile `text` into a Polars boolean expression; returns (expr, referenced columns)."""
    if not text or not text.strip():
        raise ExpressionError("the expression is empty")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(f"the expression is longer than {MAX_EXPRESSION_LENGTH} characters")
    if ";" in text:
        raise ExpressionError("only a single expression is allowed")
    try:
        tree = parse_one(text, read="postgres")
    except ParseError as exc:
        raise ExpressionError("the expression could not be parsed") from exc
    if sum(1 for _ in tree.walk()) > MAX_NODES:
        raise ExpressionError("the expression is too complex")
    compiler = _Compiler(columns)
    return compiler.compile(tree), compiler.used
