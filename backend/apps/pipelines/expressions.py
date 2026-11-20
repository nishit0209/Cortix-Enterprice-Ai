"""Safe expression builder for the `derive` step (and nothing else). NO eval, NO SQL strings.

An expression is a JSON tree built only from whitelisted operators:

    {"col": "price"}                                   column reference
    {"lit": 18}                                        literal (str, int, float, bool, null)
    {"op": "mul", "args": [{"col": "price"}, {"lit": 1.18}]}

It is validated (operator whitelist, arity, literal-only arguments, column existence, simple typing, size and
depth limits) and compiled to a Polars expression. Unknown keys are rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import polars as pl

from . import types as T

MAX_DEPTH = 12
MAX_NODES = 100
MAX_LITERAL_LEN = 1000


class ExpressionError(ValueError):
    def __init__(self, message: str, code: str = "invalid_expression"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OpSpec:
    min_args: int
    max_args: int
    result: Callable[[list[str]], str]
    arg_check: Callable[[list[str]], str | None] | None = None   # returns an error message or None
    literal_args: tuple[int, ...] = ()                             # positions that must be literals


def _numeric_result(types: list[str]) -> str:
    if all(t == T.BIGINT for t in types):
        return T.BIGINT
    return T.DOUBLE


def _need(pred: Callable[[str], bool], what: str) -> Callable[[list[str]], str | None]:
    def check(types: list[str]) -> str | None:
        bad = [t for t in types if not pred(t)]
        return f"expects {what} arguments, got {', '.join(bad)}" if bad else None

    return check


def _first_need(pred: Callable[[str], bool], what: str) -> Callable[[list[str]], str | None]:
    def check(types: list[str]) -> str | None:
        return None if pred(types[0]) else f"expects a {what} first argument, got {types[0]}"

    return check


def _same_family(types: list[str]) -> str | None:
    known = [t for t in types if t != T.UNKNOWN]
    if not known:
        return None
    if all(t in T.NUMERIC_TYPES for t in known):
        return None
    if len({t for t in known}) == 1:
        return None
    return f"cannot compare {' and '.join(sorted(set(known)))}"


def _first_type(types: list[str]) -> str:
    known = [t for t in types if t != T.UNKNOWN]
    return known[0] if known else T.UNKNOWN


_num = _need(T.is_numeric, "numeric")
_bool = _need(lambda t: t in (T.BOOLEAN, T.UNKNOWN), "boolean")

OPS: dict[str, OpSpec] = {
    # arithmetic
    "add": OpSpec(2, 10, _numeric_result, _num),
    "sub": OpSpec(2, 2, _numeric_result, _num),
    "mul": OpSpec(2, 10, _numeric_result, _num),
    "div": OpSpec(2, 2, lambda _t: T.DOUBLE, _num),
    "mod": OpSpec(2, 2, _numeric_result, _num),
    "neg": OpSpec(1, 1, _numeric_result, _num),
    "abs": OpSpec(1, 1, _numeric_result, _num),
    "round": OpSpec(1, 2, lambda _t: T.DOUBLE, _first_need(T.is_numeric, "numeric"), literal_args=(1,)),
    # comparison / logic
    "eq": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "ne": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "gt": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "gte": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "lt": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "lte": OpSpec(2, 2, lambda _t: T.BOOLEAN, _same_family),
    "and": OpSpec(2, 10, lambda _t: T.BOOLEAN, _bool),
    "or": OpSpec(2, 10, lambda _t: T.BOOLEAN, _bool),
    "not": OpSpec(1, 1, lambda _t: T.BOOLEAN, _bool),
    "is_null": OpSpec(1, 1, lambda _t: T.BOOLEAN),
    "not_null": OpSpec(1, 1, lambda _t: T.BOOLEAN),
    "coalesce": OpSpec(2, 10, _first_type, _same_family),
    "if": OpSpec(3, 3, lambda t: _first_type(t[1:]), lambda t: _bool(t[:1]) or _same_family(t[1:])),
    # text
    "concat": OpSpec(2, 10, lambda _t: T.TEXT),
    "lower": OpSpec(1, 1, lambda _t: T.TEXT, _need(T.is_text, "text")),
    "upper": OpSpec(1, 1, lambda _t: T.TEXT, _need(T.is_text, "text")),
    "trim": OpSpec(1, 1, lambda _t: T.TEXT, _need(T.is_text, "text")),
    "length": OpSpec(1, 1, lambda _t: T.BIGINT, _need(T.is_text, "text")),
    "substring": OpSpec(2, 3, lambda _t: T.TEXT, _first_need(T.is_text, "text"), literal_args=(1, 2)),
    "replace": OpSpec(3, 3, lambda _t: T.TEXT, _first_need(T.is_text, "text"), literal_args=(1, 2)),
    # dates
    "year": OpSpec(1, 1, lambda _t: T.BIGINT, _need(T.is_temporal, "date/timestamp")),
    "month": OpSpec(1, 1, lambda _t: T.BIGINT, _need(T.is_temporal, "date/timestamp")),
    "day": OpSpec(1, 1, lambda _t: T.BIGINT, _need(T.is_temporal, "date/timestamp")),
    "date_diff_days": OpSpec(2, 2, lambda _t: T.BIGINT, _need(T.is_temporal, "date/timestamp")),
    # conversion
    "cast": OpSpec(2, 2, lambda _t: T.UNKNOWN, None, literal_args=(1,)),
}


COMPARISONS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})


def _literal(node: Any) -> Any:
    if not isinstance(node, dict) or set(node) != {"lit"}:
        raise ExpressionError("This argument must be a literal ({\"lit\": ...}).", "literal_required")
    return node["lit"]


def check(node: Any, schema: T.Schema, *, _depth: int = 0, _count: list[int] | None = None) -> str:
    """Validate an expression tree against `schema` and return its logical type."""
    counter = _count if _count is not None else [0]
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise ExpressionError(f"The expression has more than {MAX_NODES} nodes.", "expression_too_large")
    if _depth > MAX_DEPTH:
        raise ExpressionError(f"The expression is nested deeper than {MAX_DEPTH} levels.", "expression_too_deep")
    if not isinstance(node, dict):
        raise ExpressionError("Each expression node must be an object.")
    keys = set(node)
    if keys == {"col"}:
        name = node["col"]
        if not isinstance(name, str) or name not in schema:
            raise ExpressionError(f"Unknown column {name!r}.", "unknown_column")
        return schema[name]
    if keys == {"lit"}:
        value = node["lit"]
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ExpressionError("Literals must be strings, numbers, booleans or null.", "invalid_literal")
        if isinstance(value, str) and len(value) > MAX_LITERAL_LEN:
            raise ExpressionError("The literal is too long.", "invalid_literal")
        return T.literal_type(value)
    if keys != {"op", "args"}:
        raise ExpressionError("Expression nodes are {\"col\"}, {\"lit\"} or {\"op\", \"args\"}.")
    op = node["op"]
    spec = OPS.get(op) if isinstance(op, str) else None
    if spec is None:
        raise ExpressionError(f"Operator {op!r} is not allowed.", "operator_not_allowed")
    args = node["args"]
    if not isinstance(args, list) or not (spec.min_args <= len(args) <= spec.max_args):
        raise ExpressionError(f"{op} takes {spec.min_args}..{spec.max_args} arguments.", "wrong_arity")
    types: list[str] = []
    for i, arg in enumerate(args):
        if i in spec.literal_args:
            value = _literal(arg)
            _check_literal_arg(op, i, value)
            types.append(T.literal_type(value))
        else:
            types.append(check(arg, schema, _depth=_depth + 1, _count=counter))
    if op in COMPARISONS:
        for i in (0, 1):  # an ISO string literal compared with a date/timestamp is typed as that column
            other = types[1 - i]
            if isinstance(args[i], dict) and isinstance(args[i].get("lit"), str) and other in T.TEMPORAL_TYPES:
                try:
                    T.coerce_literal(args[i]["lit"], pl.Date() if other == T.DATE else pl.Datetime("us", "UTC"))
                except ValueError:
                    raise ExpressionError(f"{args[i]['lit']!r} is not an ISO date/time.", "invalid_literal") from None
                types[i] = other
    if spec.arg_check is not None:
        problem = spec.arg_check(types)
        if problem:
            raise ExpressionError(f"{op} {problem}.", "type_error")
    if op == "cast":
        target = T.normalize_cast_type(_literal(args[1]))
        return target or T.UNKNOWN
    return spec.result(types)


def _check_literal_arg(op: str, pos: int, value: Any) -> None:
    if op == "round" and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 12):
        raise ExpressionError("round digits must be an integer between 0 and 12.", "invalid_literal")
    if op == "substring" and (not isinstance(value, int) or isinstance(value, bool) or value < (1 if pos == 1 else 0)):
        raise ExpressionError("substring start (1-based) and length must be non-negative integers.", "invalid_literal")
    if op == "replace" and not isinstance(value, str):
        raise ExpressionError("replace takes literal strings.", "invalid_literal")
    if op == "cast" and T.normalize_cast_type(str(value)) is None:
        raise ExpressionError(f"Unknown cast type {value!r}.", "invalid_literal")


def _to_date(e: pl.Expr) -> pl.Expr:
    return e.cast(pl.Date, strict=False)


def compile_expr(node: dict[str, Any], frame_schema: dict[str, pl.DataType]) -> pl.Expr:
    """Compile a CHECKED expression to Polars. Literals are typed against the column they meet."""
    if "col" in node:
        return pl.col(node["col"])
    if "lit" in node:
        return pl.lit(node["lit"])
    op, args = node["op"], node["args"]
    if op == "round":
        digits = int(args[1]["lit"]) if len(args) > 1 else 0
        return compile_expr(args[0], frame_schema).cast(pl.Float64).round(digits)
    if op == "substring":
        start = int(args[1]["lit"]) - 1
        length = int(args[2]["lit"]) if len(args) > 2 else None
        return compile_expr(args[0], frame_schema).cast(pl.String).str.slice(start, length)
    if op == "replace":
        return compile_expr(args[0], frame_schema).cast(pl.String).str.replace_all(
            str(args[1]["lit"]), str(args[2]["lit"]), literal=True)
    if op == "cast":
        target = T.normalize_cast_type(str(args[1]["lit"])) or T.TEXT
        source = frame_schema.get(args[0]["col"]) if "col" in args[0] else None
        return cast_expr(compile_expr(args[0], frame_schema), target, source=source)
    xs = [compile_expr(a, frame_schema) for a in args]
    match op:
        case "add":
            out = xs[0]
            for x in xs[1:]:
                out = out + x
            return out
        case "sub":
            return xs[0] - xs[1]
        case "mul":
            out = xs[0]
            for x in xs[1:]:
                out = out * x
            return out
        case "div":
            denom = xs[1].cast(pl.Float64)
            return pl.when(denom == 0).then(pl.lit(None, dtype=pl.Float64)).otherwise(xs[0].cast(pl.Float64) / denom)
        case "mod":
            return pl.when(xs[1] == 0).then(pl.lit(None)).otherwise(xs[0] % xs[1])
        case "neg":
            return -xs[0]
        case "abs":
            return xs[0].abs()
        case "eq":
            return xs[0] == _typed(args[1], xs[1], args[0], frame_schema)
        case "ne":
            return xs[0] != _typed(args[1], xs[1], args[0], frame_schema)
        case "gt":
            return xs[0] > _typed(args[1], xs[1], args[0], frame_schema)
        case "gte":
            return xs[0] >= _typed(args[1], xs[1], args[0], frame_schema)
        case "lt":
            return xs[0] < _typed(args[1], xs[1], args[0], frame_schema)
        case "lte":
            return xs[0] <= _typed(args[1], xs[1], args[0], frame_schema)
        case "and":
            return pl.all_horizontal(xs)
        case "or":
            return pl.any_horizontal(xs)
        case "not":
            return ~xs[0]
        case "is_null":
            return xs[0].is_null()
        case "not_null":
            return xs[0].is_not_null()
        case "coalesce":
            return pl.coalesce(xs)
        case "if":
            return pl.when(xs[0]).then(xs[1]).otherwise(xs[2])
        case "concat":
            return pl.concat_str([x.cast(pl.String) for x in xs], ignore_nulls=False)
        case "lower":
            return xs[0].str.to_lowercase()
        case "upper":
            return xs[0].str.to_uppercase()
        case "trim":
            return xs[0].str.strip_chars()
        case "length":
            return xs[0].str.len_chars().cast(pl.Int64)
        case "year":
            return xs[0].dt.year().cast(pl.Int64)
        case "month":
            return xs[0].dt.month().cast(pl.Int64)
        case "day":
            return xs[0].dt.day().cast(pl.Int64)
        case "date_diff_days":
            return (_to_date(xs[0]) - _to_date(xs[1])).dt.total_days().cast(pl.Int64)
    raise ExpressionError(f"Operator {op!r} is not allowed.", "operator_not_allowed")  # pragma: no cover


def _typed(lit_node: dict, compiled: pl.Expr, other: dict, frame_schema: dict[str, pl.DataType]) -> pl.Expr:
    """`{"col": d} > {"lit": "2024-01-01"}`: type a string literal as the column's date/timestamp."""
    if "lit" in lit_node and "col" in other and other["col"] in frame_schema:
        dtype = frame_schema[other["col"]]
        return pl.lit(T.coerce_literal(lit_node["lit"], dtype), dtype=dtype if lit_node["lit"] is not None else None)
    return compiled


TRUE_TOKENS = ("true", "t", "yes", "y", "1")
FALSE_TOKENS = ("false", "f", "no", "n", "0")


def cast_expr(e: pl.Expr, target: str, fmt: str | None = None, source: pl.DataType | None = None) -> pl.Expr:
    """Lenient cast (failures become null). Text inputs are parsed; typed inputs use Polars casts."""
    if target == T.TEXT:
        return e.cast(pl.String)
    if source is not None and source != pl.String and source != pl.Null:
        if target in (T.BIGINT, T.DOUBLE, T.NUMERIC, T.BOOLEAN) and (source.is_numeric() or source == pl.Boolean):
            return e.cast(T.polars_dtype(target), strict=False)
        if target == T.TIMESTAMP and source == pl.Date:
            return e.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
        if target == T.DATE and isinstance(source, pl.Datetime):
            return (e.dt.convert_time_zone("UTC") if source.time_zone else e).dt.date()
        if target == T.TIMESTAMP and isinstance(source, pl.Datetime):
            return e.dt.convert_time_zone("UTC") if source.time_zone else e.dt.replace_time_zone("UTC")
        if target == T.DATE and source == pl.Date:
            return e
    text = e.cast(pl.String).str.strip_chars()
    if target == T.BIGINT:
        as_float = text.str.replace_all(",", "", literal=True).cast(pl.Float64, strict=False)
        return pl.when(as_float.is_finite() & (as_float == as_float.floor())).then(as_float.cast(pl.Int64, strict=False)) \
            .otherwise(pl.lit(None, dtype=pl.Int64))
    if target in (T.DOUBLE, T.NUMERIC):
        as_float = text.str.replace_all(",", "", literal=True).cast(pl.Float64, strict=False)
        return pl.when(as_float.is_finite()).then(as_float).otherwise(pl.lit(None, dtype=pl.Float64))
    if target == T.BOOLEAN:
        low = text.str.to_lowercase()
        return (pl.when(low.is_in(TRUE_TOKENS)).then(pl.lit(True))
                .when(low.is_in(FALSE_TOKENS)).then(pl.lit(False))
                .otherwise(pl.lit(None, dtype=pl.Boolean)))
    if target == T.DATE:
        if fmt:
            return text.str.strptime(pl.Date, fmt, strict=False)
        iso = text.str.slice(0, 10)
        return pl.coalesce([iso.str.strptime(pl.Date, f, strict=False) for f in ("%Y-%m-%d", "%Y/%m/%d")])
    if target == T.TIMESTAMP:
        if fmt:
            parsed = text.str.strptime(pl.Datetime("us"), fmt, strict=False)
            return parsed.dt.replace_time_zone("UTC")
        norm = text.str.replace(r"^(\d{4}-\d{2}-\d{2})[Tt ]", "${1} ").str.replace(r"(?i)\s*(z|utc)$", "+00:00")
        return pl.coalesce([
            norm.str.strptime(pl.Datetime("us", "UTC"), "%Y-%m-%d %H:%M:%S%.f%#z", strict=False),
            norm.str.strptime(pl.Datetime("us"), "%Y-%m-%d %H:%M:%S%.f", strict=False).dt.replace_time_zone("UTC"),
            norm.str.strptime(pl.Datetime("us"), "%Y-%m-%d %H:%M", strict=False).dt.replace_time_zone("UTC"),
            norm.str.strptime(pl.Date, "%Y-%m-%d", strict=False).cast(pl.Datetime("us")).dt.replace_time_zone("UTC"),
        ])
    return e.cast(pl.String)
