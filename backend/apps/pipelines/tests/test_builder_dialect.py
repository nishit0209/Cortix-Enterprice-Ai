"""The pipeline builder's DAG dialect (pure, no DB): node type aliases, config key translation, text expressions.

The DAGs below are written exactly as `frontend/src/lib/pipelines/catalog.ts` + `inspector.tsx` produce them.
"""

from __future__ import annotations

import uuid

import polars as pl
import pytest

from apps.pipelines import engine, expressions
from apps.pipelines import types as T
from apps.pipelines.builder import normalize_node
from apps.pipelines.dag import Resolvers, validate_dag

DS = str(uuid.uuid4())
FRAME = pl.DataFrame({
    "order_id": [1, 2, 2, 3, 4], "city": [" Pune", "Delhi", "Delhi", None, "Mumbai "],
    "amount": [100.0, 250.0, 250.0, 40.0, 80.0], "qty": ["1", "2", "2", "x", "4"],
})


def _chain(*steps, dest=None):
    nodes = [{"id": "src", "type": "source", "config": {"dataset_id": DS}, "position": {"x": 0, "y": 0}}, *steps,
             dest or {"id": "out", "type": "destination", "config": {"dataset_name": "orders_clean",
                                                                     "mode": "new_version"}}]
    return {"nodes": nodes, "edges": [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]}


BUILDER_STEPS = [
    {"id": "trim", "type": "trim_whitespace", "config": {"columns": ["city"]}},
    {"id": "fill", "type": "fill_null", "config": {"columns": ["city"], "strategy": "constant", "value": "Unknown"}},
    {"id": "cast", "type": "cast", "config": {"casts": [{"column": "qty", "to": "bigint"}], "on_error": "null"}},
    {"id": "dedup", "type": "deduplicate", "config": {"keys": ["order_id"], "keep": "first"}},
    {"id": "big", "type": "filter", "config": {"conditions": [{"column": "amount", "op": ">=", "value": 50}],
                                               "mode": "all"}},
    {"id": "tax", "type": "derive", "config": {"name": "amount_with_tax", "expression": "round(amount * 1.18, 2)"}},
    {"id": "order", "type": "sort", "config": {"by": [{"column": "amount", "desc": True}]}},
]


def test_a_builder_dag_validates_and_runs():
    validation = validate_dag(_chain(*BUILDER_STEPS))
    assert validation.ok, validation.payload()["errors"]
    assert validation.source.type == "source.dataset" and validation.destination.type == "destination.dataset"
    assert validation.destination.config.name == "orders_clean"
    io = engine.RunIO(load_source=lambda node, limit: FRAME)
    out = engine.execute(validation, io).frame
    assert out["order_id"].to_list() == [2, 1, 4]
    assert out["amount_with_tax"].to_list() == [295.0, 118.0, 94.4]
    assert out["city"].to_list() == ["Delhi", "Pune", "Mumbai"] and out["qty"].dtype == pl.Int64


def test_builder_aggregate_and_canonical_dags_are_unchanged():
    agg = {"id": "agg", "type": "aggregate", "config": {"group_by": ["city"], "aggregations": [
        {"column": "amount", "fn": "avg", "as": "avg_amount"}, {"column": "", "fn": "count", "as": ""}]}}
    validation = validate_dag(_chain(agg))
    assert validation.ok, validation.payload()["errors"]
    out = engine.execute(validation, engine.RunIO(load_source=lambda node, limit: FRAME)).frame
    assert "avg_amount" in out.columns and out.height == 4
    canonical = {"id": "s", "type": "sort", "config": {"by": [{"column": "amount", "descending": False}]}}
    assert normalize_node("sort", canonical["config"]) == ("sort", canonical["config"], [])
    assert normalize_node("source.dataset", {"dataset_id": DS}) == ("source.dataset", {"dataset_id": DS}, [])


def test_explicit_canonical_keys_win_over_builder_keys():
    _t, cfg, problems = normalize_node("deduplicate", {"keys": ["city"], "subset": ["order_id"]})
    assert cfg["subset"] == ["order_id"] and problems == []
    _t, cfg, _p = normalize_node("filter", {"conditions": [], "mode": "any", "match": "all"})
    assert cfg["match"] == "all"


def test_append_mode_and_bad_expressions_are_pinned_on_their_node():
    dest = {"id": "out", "type": "destination", "config": {"dataset_name": "x", "mode": "append"}}
    bad = {"id": "tax", "type": "derive", "config": {"name": "t", "expression": "amount * (1.18"}}
    errors = validate_dag(_chain(bad, dest=dest)).payload()["errors"]
    by_node = {(e["node_id"], e["field"]): e["code"] for e in errors}
    assert by_node[("out", "nodes.out.config.mode")] == "unsupported_mode"
    assert by_node[("tax", "nodes.tax.config.expression")] == "invalid_expression"


def test_dotted_design_names_are_accepted():
    steps = [{"id": "t", "type": "clean.trim_whitespace", "config": {}},
             {"id": "d", "type": "dedup.exact", "config": {}}]
    dag = _chain(*steps)
    dag["nodes"][0]["type"] = "source.dataset"
    assert validate_dag(dag).ok


# ------------------------------------------------------------------------------------------------ text expressions
@pytest.mark.parametrize(("text", "tree"), [
    ("amount - discount", {"op": "sub", "args": [{"col": "amount"}, {"col": "discount"}]}),
    ("a + b + c", {"op": "add", "args": [{"col": "a"}, {"col": "b"}, {"col": "c"}]}),
    ("a - b - c", {"op": "sub", "args": [{"op": "sub", "args": [{"col": "a"}, {"col": "b"}]}, {"col": "c"}]}),
    ("a + b * c", {"op": "add", "args": [{"col": "a"}, {"op": "mul", "args": [{"col": "b"}, {"col": "c"}]}]}),
    ("(a + b) * -2", {"op": "mul", "args": [{"op": "add", "args": [{"col": "a"}, {"col": "b"}]}, {"lit": -2}]}),
    ("round(price * 1.18, 2)", {"op": "round", "args": [
        {"op": "mul", "args": [{"col": "price"}, {"lit": 1.18}]}, {"lit": 2}]}),
    ("qty >= 2 and not is_null(city)", {"op": "and", "args": [
        {"op": "gte", "args": [{"col": "qty"}, {"lit": 2}]},
        {"op": "not", "args": [{"op": "is_null", "args": [{"col": "city"}]}]}]}),
    ("if(qty > 1, 'bulk', \"single\")", {"op": "if", "args": [
        {"op": "gt", "args": [{"col": "qty"}, {"lit": 1}]}, {"lit": "bulk"}, {"lit": "single"}]}),
    ("concat(`first name`, ' ', 'O''Brien')", {"op": "concat", "args": [
        {"col": "first name"}, {"lit": " "}, {"lit": "O'Brien"}]}),
    ("cast(qty, 'integer') <> NULL", {"op": "ne", "args": [
        {"op": "cast", "args": [{"col": "qty"}, {"lit": "integer"}]}, {"lit": None}]}),
    ("-amount", {"op": "neg", "args": [{"col": "amount"}]}),
    ("1e3 + .5", {"op": "add", "args": [{"lit": 1000.0}, {"lit": 0.5}]}),
])
def test_parse_text(text, tree):
    assert expressions.parse_text(text) == tree


@pytest.mark.parametrize("text", [
    "", "   ", "amount *", "(a + b", "a b", "pg_sleep(1)", "__import__('os')", "a; DROP TABLE x", "a @ b",
    "x" * (expressions.MAX_TEXT_LEN + 1),
])
def test_parse_text_rejects(text):
    with pytest.raises(expressions.ExpressionError):
        expressions.parse_text(text)


def test_parsed_trees_are_still_checked_against_the_schema():
    schema = {"amount": "double precision", "city": "text"}
    assert expressions.check(expressions.parse_text("round(amount * 1.18, 2)"), schema) == "double precision"
    with pytest.raises(expressions.ExpressionError):
        expressions.check(expressions.parse_text("amount + city"), schema)  # type error
    with pytest.raises(expressions.ExpressionError):
        expressions.check(expressions.parse_text("secret * 2"), schema)  # unknown column
    long_sum = " + ".join(f"c{i}" for i in range(12))  # more operands than add's arity: stays nested, still valid
    tree = expressions.parse_text(long_sum)
    assert expressions.check(tree, {f"c{i}": "bigint" for i in range(12)}) == "bigint"


# ------------------------------------------------------------------- configs exactly as the inspector emits them
# (review fix W3): the inspector keeps hidden defaults such as value "" and offers "*" as the count(*) column.
RESOLVERS = Resolvers(dataset_schema=lambda i: {"order_id": T.BIGINT, "city": T.TEXT, "amount": T.DOUBLE,
                                                "qty": T.TEXT} if str(i) == DS else None,
                      check_dataset=lambda i: None)
GAPS = pl.DataFrame({"order_id": [1, 2, 3, 4], "city": ["Pune", None, "Pune", None],
                     "amount": [10.0, None, 30.0, None], "qty": ["1", "2", "3", "4"]})


def _run(*steps):
    validation = validate_dag(_chain(*steps), RESOLVERS)
    assert validation.ok, validation.payload()["errors"]
    return engine.execute(validation, engine.RunIO(load_source=lambda node, limit: GAPS)).frame


@pytest.mark.parametrize(("strategy", "column", "value", "expected"), [
    ("constant", "city", "Unknown", ["Pune", "Unknown", "Pune", "Unknown"]),
    ("constant", "city", "", ["Pune", "", "Pune", ""]),
    ("mean", "amount", "", [10.0, 20.0, 30.0, 20.0]),
    ("median", "amount", "", [10.0, 20.0, 30.0, 20.0]),
    ("mode", "city", "", ["Pune", "Pune", "Pune", "Pune"]),
    ("forward_fill", "amount", "", [10.0, 10.0, 30.0, 30.0]),
    ("backward_fill", "amount", None, [10.0, 30.0, 30.0, None]),
])
def test_every_builder_fill_strategy_validates_and_runs(strategy, column, value, expected):
    fill = {"id": "f", "type": "fill_null", "config": {"columns": [column], "strategy": strategy, "value": value}}
    assert _run(fill)[column].to_list() == expected


def test_a_real_value_with_a_statistic_strategy_is_still_rejected():
    fill = {"id": "f", "type": "fill_null", "config": {"columns": ["amount"], "strategy": "mean", "value": "7"}}
    errors = validate_dag(_chain(fill), RESOLVERS).payload()["errors"]
    assert [e["node_id"] for e in errors] == ["f"]


@pytest.mark.parametrize(("op", "expected"), [("is_null", [2, 4]), ("not_null", [1, 3])])
def test_null_filters_from_the_inspector_carry_an_empty_value(op, expected):
    flt = {"id": "n", "type": "filter", "config": {"conditions": [{"column": "city", "op": op, "value": ""}],
                                                   "mode": "all"}}
    assert _run(flt)["order_id"].to_list() == expected


def test_a_real_value_on_a_null_filter_is_still_rejected():
    flt = {"id": "n", "type": "filter", "config": {"conditions": [{"column": "city", "op": "is_null",
                                                                   "value": "x"}], "mode": "all"}}
    assert not validate_dag(_chain(flt), RESOLVERS).ok


def test_count_star_from_the_inspector():
    agg = {"id": "agg", "type": "aggregate", "config": {"group_by": ["city"], "aggregations": [
        {"column": "*", "fn": "count", "as": "orders"}, {"column": "amount", "fn": "sum", "as": ""}]}}
    out = _run(agg).sort("city", nulls_last=True)
    assert out["orders"].to_list() == [2, 2]


def test_star_is_only_valid_for_count():
    agg = {"id": "agg", "type": "aggregate", "config": {"group_by": [], "aggregations": [
        {"column": "*", "fn": "sum", "as": ""}]}}
    errors = validate_dag(_chain(agg), RESOLVERS).payload()["errors"]
    assert [e["node_id"] for e in errors] == ["agg"]


@pytest.mark.parametrize("text", [
    "(" * 150 + "1" + ")" * 150, "-" * 1500 + "a", "not " * 400 + "a", "abs(" * 100 + "a" + ")" * 100,
    "(" * (expressions.MAX_PARSE_DEPTH + 1) + "a" + ")" * (expressions.MAX_PARSE_DEPTH + 1),
])
def test_deep_nesting_is_an_expression_error_not_a_recursion_error(text):
    with pytest.raises(expressions.ExpressionError) as exc:
        expressions.parse_text(text)
    assert exc.value.code == "expression_too_deep"
    derive = {"id": "d", "type": "derive", "config": {"name": "x", "expression": text}}
    errors = validate_dag(_chain(derive), RESOLVERS).payload()["errors"]
    assert {(e["node_id"], e["code"]) for e in errors} == {("d", "expression_too_deep")}


def test_nesting_up_to_the_parse_bound_still_parses():
    depth = expressions.MAX_PARSE_DEPTH
    assert expressions.parse_text("(" * depth + "a" + ")" * depth) == {"col": "a"}
    assert expressions.parse_text("--a") == {"op": "neg", "args": [{"op": "neg", "args": [{"col": "a"}]}]}
