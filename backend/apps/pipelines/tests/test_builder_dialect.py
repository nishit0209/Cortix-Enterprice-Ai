"""The pipeline builder's DAG dialect (pure, no DB): node type aliases, config key translation, text expressions.

The DAGs below are written exactly as `frontend/src/lib/pipelines/catalog.ts` + `inspector.tsx` produce them.
"""

from __future__ import annotations

import uuid

import polars as pl
import pytest

from apps.pipelines import engine, expressions
from apps.pipelines.builder import normalize_node
from apps.pipelines.dag import validate_dag

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
