"""Safe expression builder for `derive`: whitelist, typing, limits and Polars evaluation."""

import datetime as dt

import polars as pl
import pytest

from apps.pipelines import expressions as E
from apps.pipelines import types as T

SCHEMA = {"price": T.DOUBLE, "qty": T.BIGINT, "name": T.TEXT, "ok": T.BOOLEAN, "d1": T.DATE, "d2": T.DATE}
FRAME = pl.DataFrame({
    "price": [10.0, 2.5, None], "qty": [3, 4, 5], "name": [" Ann ", "bob", None], "ok": [True, False, None],
    "d1": [dt.date(2024, 1, 10), dt.date(2024, 3, 1), None], "d2": [dt.date(2024, 1, 1), dt.date(2024, 1, 1), None],
})


def ev(expr):
    E.check(expr, SCHEMA)
    return FRAME.with_columns(E.compile_expr(expr, dict(FRAME.schema)).alias("x"))["x"].to_list()


def col(n):
    return {"col": n}


def lit(v):
    return {"lit": v}


def op(name, *args):
    return {"op": name, "args": list(args)}


@pytest.mark.parametrize("expr,expected", [
    (op("add", col("qty"), lit(1)), [4, 5, 6]),
    (op("sub", col("qty"), lit(1)), [2, 3, 4]),
    (op("mul", col("price"), col("qty")), [30.0, 10.0, None]),
    (op("div", col("price"), lit(0)), [None, None, None]),
    (op("div", col("qty"), lit(2)), [1.5, 2.0, 2.5]),
    (op("mod", col("qty"), lit(2)), [1, 0, 1]),
    (op("neg", col("qty")), [-3, -4, -5]),
    (op("abs", op("neg", col("qty"))), [3, 4, 5]),
    (op("round", op("div", col("qty"), lit(3)), lit(2)), [1.0, 1.33, 1.67]),
    (op("gt", col("qty"), lit(3)), [False, True, True]),
    (op("and", op("gte", col("qty"), lit(4)), op("lt", col("qty"), lit(5))), [False, True, False]),
    (op("or", op("eq", col("qty"), lit(3)), op("eq", col("qty"), lit(5))), [True, False, True]),
    (op("not", col("ok")), [False, True, None]),
    (op("is_null", col("name")), [False, False, True]),
    (op("coalesce", col("name"), lit("n/a")), [" Ann ", "bob", "n/a"]),
    (op("if", op("gt", col("qty"), lit(3)), lit("big"), lit("small")), ["small", "big", "big"]),
    (op("concat", op("trim", col("name")), lit("-"), col("qty")), ["Ann-3", "bob-4", None]),
    (op("upper", col("name")), [" ANN ", "BOB", None]),
    (op("lower", lit("ABC")), ["abc", "abc", "abc"]),
    (op("length", op("trim", col("name"))), [3, 3, None]),
    (op("substring", col("name"), lit(2), lit(3)), ["Ann", "ob", None]),
    (op("replace", col("name"), lit("b"), lit("B")), [" Ann ", "BoB", None]),
    (op("year", col("d1")), [2024, 2024, None]),
    (op("month", col("d1")), [1, 3, None]),
    (op("day", col("d1")), [10, 1, None]),
    (op("date_diff_days", col("d1"), col("d2")), [9, 60, None]),
    (op("gt", col("d1"), lit("2024-02-01")), [False, True, None]),
    (op("cast", col("qty"), lit("text")), ["3", "4", "5"]),
    (op("cast", lit("12"), lit("integer")), [12, 12, 12]),
])
def test_operators_evaluate(expr, expected):
    out = ev(expr)
    assert [round(v, 2) if isinstance(v, float) else v for v in out] == expected


@pytest.mark.parametrize("expr,code", [
    ({"op": "exec", "args": [lit("rm -rf")]}, "operator_not_allowed"),
    ({"op": "__import__", "args": [lit("os")]}, "operator_not_allowed"),
    ({"sql": "select 1"}, "invalid_expression"),
    ({"col": "missing"}, "unknown_column"),
    ({"col": "qty", "extra": 1}, "invalid_expression"),
    ({"lit": {"nested": "obj"}}, "invalid_literal"),
    (op("add", col("qty")), "wrong_arity"),
    (op("add", col("name"), lit(1)), "type_error"),
    (op("and", col("qty"), col("ok")), "type_error"),
    (op("eq", col("qty"), col("name")), "type_error"),
    (op("round", col("price"), col("qty")), "literal_required"),
    (op("round", col("price"), lit(99)), "invalid_literal"),
    (op("cast", col("qty"), lit("blob")), "invalid_literal"),
    (op("year", col("name")), "type_error"),
    ("not an object", "invalid_expression"),
])
def test_rejections(expr, code):
    with pytest.raises(E.ExpressionError) as e:
        E.check(expr, SCHEMA)
    assert e.value.code == code


def test_depth_and_size_limits():
    deep = col("qty")
    for _ in range(E.MAX_DEPTH + 2):
        deep = op("neg", deep)
    with pytest.raises(E.ExpressionError) as e:
        E.check(deep, SCHEMA)
    assert e.value.code == "expression_too_deep"
    wide = op("add", *[op("add", col("qty"), lit(1)) for _ in range(10)])
    big = op("add", *[wide for _ in range(10)])
    with pytest.raises(E.ExpressionError) as e2:
        E.check(big, SCHEMA)
    assert e2.value.code == "expression_too_large"


def test_result_types():
    assert E.check(op("add", col("qty"), lit(1)), SCHEMA) == T.BIGINT
    assert E.check(op("add", col("qty"), lit(1.5)), SCHEMA) == T.DOUBLE
    assert E.check(op("concat", col("name"), col("qty")), SCHEMA) == T.TEXT
    assert E.check(op("cast", col("name"), lit("date")), SCHEMA) == T.DATE
    assert E.check(op("if", col("ok"), col("qty"), lit(0)), SCHEMA) == T.BIGINT
