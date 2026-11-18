"""Every transform step, pure Polars (no DB). Synthetic data only."""

import datetime as dt
import uuid

import polars as pl
import pytest

from apps.pipelines import steps as S
from apps.pipelines import types as T


def run(step_type, frame, config, ctx=None):
    step = S.STEPS[step_type]
    cfg = step.parse(config)
    return step.apply(frame, cfg, ctx or S.StepContext())


def infer(step_type, schema, config, ctx=None):
    step = S.STEPS[step_type]
    return step.infer(schema, step.parse(config), ctx or S.StepContext())


@pytest.fixture
def orders():
    return pl.DataFrame({
        "order_id": [1, 2, 3, 4, 5],
        "city": [" Pune ", "mumbai", None, "Delhi", "pune"],
        "amount": [100.0, None, 250.5, 80.0, 100.0],
        "qty": [1, 2, None, 4, 1],
        "placed": [dt.date(2024, 1, 5), dt.date(2024, 2, 1), dt.date(2024, 2, 10), None, dt.date(2024, 3, 1)],
        "raw_qty": ["1", "2", "x", " 4 ", ""],
    })


SCHEMA = {"order_id": T.BIGINT, "city": T.TEXT, "amount": T.DOUBLE, "qty": T.BIGINT, "placed": T.DATE,
          "raw_qty": T.TEXT}


def test_registry_has_every_mission_step():
    assert set(S.STEPS) == {"select_columns", "rename", "cast", "filter", "fill_null", "drop_nulls", "deduplicate",
                            "trim_whitespace", "change_case", "derive", "sort", "aggregate", "join"}


def test_unknown_config_keys_are_rejected():
    with pytest.raises(Exception):
        S.STEPS["select_columns"].parse({"columns": ["a"], "sql": "drop table x"})


# --- select / rename ---------------------------------------------------------------------------------
def test_select_columns_keeps_order(orders):
    out = run("select_columns", orders, {"columns": ["city", "order_id"]})
    assert out.frame.columns == ["city", "order_id"]
    assert infer("select_columns", SCHEMA, {"columns": ["city", "order_id"]}) == {"city": T.TEXT, "order_id": T.BIGINT}


def test_select_unknown_column_is_a_config_error():
    with pytest.raises(S.StepConfigError) as e:
        infer("select_columns", SCHEMA, {"columns": ["nope"]})
    assert e.value.code == "unknown_column"


def test_rename(orders):
    out = run("rename", orders, {"mapping": {"city": "town"}})
    assert "town" in out.frame.columns and "city" not in out.frame.columns
    assert infer("rename", SCHEMA, {"mapping": {"city": "town"}})["town"] == T.TEXT


def test_rename_to_existing_name_is_rejected():
    with pytest.raises(S.StepConfigError) as e:
        infer("rename", SCHEMA, {"mapping": {"city": "qty"}})
    assert e.value.code == "duplicate_column"


# --- cast ----------------------------------------------------------------------------------------------
def test_cast_text_to_integer_with_null_policy(orders):
    out = run("cast", orders, {"columns": {"raw_qty": "integer"}})
    assert out.frame["raw_qty"].to_list() == [1, 2, None, 4, None]
    assert out.frame.schema["raw_qty"] == pl.Int64
    assert out.rejected == 0


def test_cast_reject_removes_failed_rows_and_counts_them(orders):
    out = run("cast", orders, {"columns": {"raw_qty": "integer"}, "on_error": "reject"})
    assert out.rejected == 1  # "x" fails; "" is treated as missing, not a failure
    assert out.frame.height == 4


def test_cast_fail_raises(orders):
    with pytest.raises(S.StepRuntimeError) as e:
        run("cast", orders, {"columns": {"raw_qty": "integer"}, "on_error": "fail"})
    assert e.value.code == "cast_failed"


def test_cast_dates_booleans_and_numbers():
    frame = pl.DataFrame({"d": ["2024-01-31", "31/01/2024", None], "b": ["yes", "N", "maybe"],
                          "n": ["1,234.5", "-2", "abc"], "ts": ["2024-01-31T10:00:00Z", "2024-01-31 10:00", "x"]})
    out = run("cast", frame, {"columns": {"d": "date", "b": "boolean", "n": "float", "ts": "timestamp"}})
    assert out.frame["d"].to_list() == [dt.date(2024, 1, 31), None, None]
    assert out.frame["b"].to_list() == [True, False, None]
    assert out.frame["n"].to_list() == [1234.5, -2.0, None]
    assert out.frame["ts"][0] == dt.datetime(2024, 1, 31, 10, tzinfo=dt.UTC)
    assert out.frame["ts"][1] == dt.datetime(2024, 1, 31, 10, tzinfo=dt.UTC)
    assert out.frame["ts"][2] is None


def test_cast_with_explicit_format():
    frame = pl.DataFrame({"d": ["31/01/2024", "01/02/2024"]})
    out = run("cast", frame, {"columns": {"d": "date"}, "formats": {"d": "%d/%m/%Y"}})
    assert out.frame["d"].to_list() == [dt.date(2024, 1, 31), dt.date(2024, 2, 1)]


def test_cast_typed_inputs_use_polars_casts(orders):
    out = run("cast", orders, {"columns": {"qty": "float", "order_id": "text", "placed": "timestamp"}})
    assert out.frame.schema["qty"] == pl.Float64
    assert out.frame["order_id"].to_list()[0] == "1"
    assert out.frame["placed"][0] == dt.datetime(2024, 1, 5, tzinfo=dt.UTC)


def test_cast_unknown_type_is_a_config_error():
    with pytest.raises(S.StepConfigError) as e:
        infer("cast", SCHEMA, {"columns": {"qty": "blob"}})
    assert e.value.code == "unknown_type"


# --- filter ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("cond,expected", [
    ({"column": "amount", "op": "gt", "value": 90}, [1, 3, 5]),
    ({"column": "amount", "op": "lte", "value": 80}, [4]),
    ({"column": "qty", "op": "eq", "value": 1}, [1, 5]),
    ({"column": "qty", "op": "ne", "value": 1}, [2, 4]),
    ({"column": "order_id", "op": "in", "value": [2, 4, 9]}, [2, 4]),
    ({"column": "order_id", "op": "not_in", "value": [2, 4]}, [1, 3, 5]),
    ({"column": "city", "op": "is_null"}, [3]),
    ({"column": "city", "op": "not_null"}, [1, 2, 4, 5]),
    ({"column": "city", "op": "contains", "value": "un"}, [1, 5]),
    ({"column": "city", "op": "starts_with", "value": "mum"}, [2]),
    ({"column": "city", "op": "ends_with", "value": "hi"}, [4]),
    ({"column": "city", "op": "eq", "value": "PUNE", "case_sensitive": False}, [5]),
    ({"column": "amount", "op": "between", "value": [80, 100]}, [1, 4, 5]),
    ({"column": "placed", "op": "gte", "value": "2024-02-01"}, [2, 3, 5]),
    ({"column": "placed", "op": "between", "value": ["2024-01-01", "2024-01-31"]}, [1]),
])
def test_filter_operators(orders, cond, expected):
    out = run("filter", orders, {"conditions": [cond]})
    assert out.frame["order_id"].to_list() == expected


def test_filter_match_any(orders):
    out = run("filter", orders, {"match": "any", "conditions": [
        {"column": "order_id", "op": "eq", "value": 1}, {"column": "city", "op": "is_null"}]})
    assert out.frame["order_id"].to_list() == [1, 3]


def test_filter_rejects_sql_strings_and_bad_shapes():
    for bad in ({"conditions": [{"column": "a", "op": "sql", "value": "1=1"}]},
                {"conditions": [{"column": "a", "op": "in", "value": 3}]},
                {"conditions": [{"column": "a", "op": "between", "value": [1]}]},
                {"conditions": [{"column": "a", "op": "is_null", "value": 3}]},
                {"conditions": []},
                {"where": "a > 1"}):
        with pytest.raises(Exception):
            S.STEPS["filter"].parse(bad)


def test_filter_type_checks_against_schema():
    with pytest.raises(S.StepConfigError) as e:
        infer("filter", SCHEMA, {"conditions": [{"column": "amount", "op": "gt", "value": "lots"}]})
    assert e.value.code == "type_error"
    with pytest.raises(S.StepConfigError):
        infer("filter", SCHEMA, {"conditions": [{"column": "placed", "op": "gt", "value": "yesterday"}]})


# --- cleaning ----------------------------------------------------------------------------------------------
def test_fill_null_strategies(orders):
    assert run("fill_null", orders, {"columns": ["amount"], "strategy": "zero"}).frame["amount"][1] == 0
    assert run("fill_null", orders, {"columns": ["amount"], "strategy": "value", "value": 5}).frame["amount"][1] == 5
    mean = run("fill_null", orders, {"columns": ["amount"], "strategy": "mean"}).frame["amount"][1]
    assert mean == pytest.approx((100 + 250.5 + 80 + 100) / 4)
    assert run("fill_null", orders, {"columns": ["qty"], "strategy": "median"}).frame["qty"][2] == 1
    assert run("fill_null", orders, {"columns": ["qty"], "strategy": "mode"}).frame["qty"][2] == 1
    assert run("fill_null", orders, {"columns": ["city"], "strategy": "forward"}).frame["city"][2] == "mumbai"
    assert run("fill_null", orders, {"columns": ["city"], "strategy": "backward"}).frame["city"][2] == "Delhi"
    assert run("fill_null", orders, {"columns": ["city"], "strategy": "value", "value": "?"}).frame["city"][2] == "?"


def test_fill_null_type_checks():
    with pytest.raises(S.StepConfigError):
        infer("fill_null", SCHEMA, {"columns": ["city"], "strategy": "mean"})
    with pytest.raises(S.StepConfigError):
        infer("fill_null", SCHEMA, {"columns": ["qty"], "strategy": "value", "value": "x"})
    with pytest.raises(Exception):
        S.STEPS["fill_null"].parse({"columns": ["qty"], "strategy": "value"})


def test_drop_nulls(orders):
    assert run("drop_nulls", orders, {"columns": ["city"]}).frame.height == 4
    assert run("drop_nulls", orders, {}).frame["order_id"].to_list() == [1, 5]  # any null -> dropped ("" is a value)
    assert run("drop_nulls", orders, {"columns": ["city", "qty"], "how": "all"}).frame.height == 4


def test_deduplicate(orders):
    out = run("deduplicate", orders, {"subset": ["amount", "qty"]})
    assert out.frame["order_id"].to_list() == [1, 2, 3, 4]
    last = run("deduplicate", orders, {"subset": ["amount", "qty"], "keep": "last"})
    assert 5 in last.frame["order_id"].to_list() and 1 not in last.frame["order_id"].to_list()
    exact = run("deduplicate", pl.concat([orders, orders.head(2)]), {})
    assert exact.frame.height == 5
    ordered = run("deduplicate", orders, {"subset": ["qty"], "order_by": "order_id", "descending": True})
    assert ordered.frame.filter(pl.col("qty") == 1)["order_id"].to_list() == [5]


def test_trim_whitespace_all_text_columns(orders):
    out = run("trim_whitespace", orders, {})
    assert out.frame["city"][0] == "Pune" and out.frame["raw_qty"][3] == "4"


def test_trim_rejects_non_text_column():
    with pytest.raises(S.StepConfigError):
        infer("trim_whitespace", SCHEMA, {"columns": ["qty"]})


def test_change_case(orders):
    assert run("change_case", orders, {"columns": ["city"], "case": "upper"}).frame["city"][1] == "MUMBAI"
    assert run("change_case", orders, {"columns": ["city"], "case": "lower"}).frame["city"][3] == "delhi"
    assert run("change_case", orders, {"columns": ["city"], "case": "title"}).frame["city"][1] == "Mumbai"


# --- reshape -----------------------------------------------------------------------------------------------
def test_derive_expression(orders):
    expr = {"op": "mul", "args": [{"col": "amount"}, {"lit": 2}]}
    out = run("derive", orders, {"name": "double_amount", "expression": expr})
    assert out.frame["double_amount"].to_list() == [200.0, None, 501.0, 160.0, 200.0]
    assert infer("derive", SCHEMA, {"name": "double_amount", "expression": expr})["double_amount"] == T.DOUBLE


def test_derive_refuses_overwrite_without_replace():
    with pytest.raises(S.StepConfigError) as e:
        infer("derive", SCHEMA, {"name": "qty", "expression": {"lit": 1}})
    assert e.value.code == "duplicate_column"
    assert infer("derive", SCHEMA, {"name": "qty", "expression": {"lit": 1}, "replace": True})["qty"] == T.BIGINT


def test_sort(orders):
    out = run("sort", orders, {"by": [{"column": "amount", "descending": True}]})
    assert out.frame["order_id"].to_list() == [3, 1, 5, 4, 2]  # nulls last
    multi = run("sort", orders, {"by": [{"column": "qty"}, {"column": "order_id", "descending": True}]})
    assert multi.frame["order_id"].to_list()[:2] == [5, 1]


def test_aggregate(orders):
    cfg = {"group_by": ["qty"], "aggregations": [
        {"func": "count"}, {"func": "sum", "column": "amount", "alias": "total"},
        {"func": "count_distinct", "column": "city"}, {"func": "max", "column": "placed"}]}
    out = run("aggregate", orders, cfg).frame.sort("qty", nulls_last=True)
    assert out.columns == ["qty", "row_count", "total", "city_count_distinct", "placed_max"]
    first = out.row(0, named=True)
    assert first["qty"] == 1 and first["row_count"] == 2 and first["total"] == 200.0
    schema = infer("aggregate", SCHEMA, cfg)
    assert schema == {"qty": T.BIGINT, "row_count": T.BIGINT, "total": T.DOUBLE, "city_count_distinct": T.BIGINT,
                      "placed_max": T.DATE}


def test_aggregate_without_group_by(orders):
    out = run("aggregate", orders, {"aggregations": [{"func": "mean", "column": "amount"}]})
    assert out.frame.height == 1


def test_aggregate_type_and_name_checks():
    with pytest.raises(S.StepConfigError):
        infer("aggregate", SCHEMA, {"aggregations": [{"func": "sum", "column": "city"}]})
    with pytest.raises(S.StepConfigError):
        infer("aggregate", SCHEMA, {"aggregations": [{"func": "count"}, {"func": "count"}]})
    with pytest.raises(Exception):
        S.STEPS["aggregate"].parse({"aggregations": [{"func": "sum"}]})


def test_join_with_another_dataset(orders):
    cities = pl.DataFrame({"name": ["Pune", "Delhi", "Mumbai"], "state": ["MH", "DL", "MH"], "qty": [9, 9, 9]})
    ds = uuid.uuid4()
    calls = []

    def load(dataset_id, limit):
        calls.append((dataset_id, limit))
        return cities

    ctx = S.StepContext(load_dataset=load)
    trimmed = run("trim_whitespace", orders, {}).frame
    cfg = {"dataset_id": str(ds), "how": "left", "on": [{"left": "city", "right": "name"}]}
    out = run("join", trimmed, cfg, ctx).frame
    assert out.columns == [*orders.columns, "state", "qty_right"]
    assert out.filter(pl.col("order_id") == 1)["state"].to_list() == ["MH"]
    assert out.filter(pl.col("order_id") == 2)["state"].to_list() == [None]  # "mumbai" != "Mumbai"
    assert calls == [(ds, None)] and ctx.inputs[0]["dataset_id"] == str(ds)
    inner = run("join", trimmed, {**cfg, "how": "inner", "columns": ["state"]}, S.StepContext(load_dataset=load))
    assert inner.frame.columns == [*orders.columns, "state"] and inner.frame.height == 2


def test_join_schema_inference_uses_right_schema():
    right = {"name": T.TEXT, "state": T.TEXT, "qty": T.BIGINT}
    ctx = S.StepContext(dataset_schema=lambda _id: right)
    cfg = {"dataset_id": str(uuid.uuid4()), "on": [{"left": "city", "right": "name"}]}
    out = infer("join", SCHEMA, cfg, ctx)
    assert out["state"] == T.TEXT and out["qty_right"] == T.BIGINT
    with pytest.raises(S.StepConfigError) as e:
        infer("join", SCHEMA, {**cfg, "on": [{"left": "qty", "right": "name"}]}, ctx)
    assert e.value.code == "type_error"
    missing = S.StepContext(dataset_schema=lambda _id: None)
    with pytest.raises(S.StepConfigError) as e2:
        infer("join", SCHEMA, cfg, missing)
    assert e2.value.code == "dataset_not_found"


def test_join_aligns_numeric_key_types():
    left = pl.DataFrame({"k": [1, 2]})
    right = pl.DataFrame({"k": [1.0, 2.0], "v": ["a", "b"]})
    ctx = S.StepContext(load_dataset=lambda _i, _l: right)
    out = run("join", left, {"dataset_id": str(uuid.uuid4()), "on": [{"left": "k", "right": "k"}]}, ctx)
    assert out.frame["v"].to_list() == ["a", "b"]
