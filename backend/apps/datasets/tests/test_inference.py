"""Type ladder, casting and column naming (no DB)."""

import datetime as dt

import polars as pl
import pytest

from apps.datasets.ingest import inference
from apps.datasets.ingest.columns import normalize_column_names, to_snake


def plan(values, **kw):
    return inference.infer_plan(inference.clean_series(pl.Series(values, dtype=pl.String)).drop_nulls(), **kw)


def cast(values, p):
    return inference.cast_column(pl.Series(values, dtype=pl.String), p)


# --- ladder -----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "values,expected",
    [
        (["true", "False", "YES", "n", None], "boolean"),
        (["1", "-2", "+3", "40000000000"], "bigint"),
        (["0", "1", "0"], "bigint"),
        (["1.5", "2", "-3.25e2"], "double precision"),
        (["1,234", "12,345,678", "7"], "bigint"),
        (["₹1,23,456.50", "₹2,000", "Rs. 300"], "numeric"),
        (["$1,200.00", "$15.5", "-$3"], "numeric"),
        (["12.5%", "5%", "100%"], "double precision"),
        (["(1,234.50)", "200.25"], "double precision"),
        (["2024-01-05", "2024-1-6", "2023/12/31"], "date"),
        (["31/01/2024", "05/02/2024"], "date"),
        (["2024-01-05T10:00:00Z", "2024-01-06 11:30:00+05:30", "2024-01-07 09:00"], "timestamptz"),
        (["007", "008", "009"], "text"),
        (["0987654321", "0912345678"], "text"),
        (["hello", "world"], "text"),
        (["1", "2", "x"], "text"),
        (["12345678901234567890"], "text"),
        ([], "text"),
    ],
)
def test_type_ladder(values, expected):
    assert plan(values).pg_type == expected


def test_995_rule_tolerates_a_few_bad_values():
    values = [str(i) for i in range(1000)] + ["oops"]
    p = plan(values)
    assert p.pg_type == "bigint"
    res = cast(values, p)
    assert res.invalid_count == 1
    assert res.series.dtype == pl.Int64 and res.series[1000] is None


def test_995_rule_rejects_too_many_bad_values():
    values = [str(i) for i in range(100)] + ["oops"]  # 99.0% < 99.5%
    assert plan(values).pg_type == "text"


def test_null_tokens_are_ignored_for_typed_columns():
    values = ["1", "NA", "n/a", "", "null", "  ", "2", "None", "#N/A"]
    p = plan(values)
    assert p.pg_type == "bigint"
    res = cast(values, p)
    assert res.invalid_count == 0
    assert res.series.to_list() == [1, None, None, None, None, None, 2, None, None]


def test_text_columns_keep_values_but_blank_becomes_null():
    res = cast(["NA", " x ", "", "  "], inference.TypePlan("text"))
    assert res.series.to_list() == ["NA", " x ", None, None]


def test_day_first_is_preferred_for_ambiguous_dates_and_mdy_detected_when_needed():
    p = plan(["01/02/2024", "03/04/2024"])
    assert cast(["01/02/2024"], p).series[0] == dt.date(2024, 2, 1)
    p2 = plan(["12/31/2024", "01/15/2024"])
    assert cast(["12/31/2024"], p2).series[0] == dt.date(2024, 12, 31)


def test_timestamps_are_normalized_to_utc():
    p = plan(["2024-01-05T10:00:00Z", "2024-01-05 10:00:00+05:30", "2024-01-05"])
    s = cast(["2024-01-05T10:00:00Z", "2024-01-05 10:00:00+05:30", "2024-01-05"], p).series
    assert s.dtype == pl.Datetime("us", "UTC")
    assert s[0] == dt.datetime(2024, 1, 5, 10, tzinfo=dt.UTC)
    assert s[1] == dt.datetime(2024, 1, 5, 4, 30, tzinfo=dt.UTC)
    assert s[2] == dt.datetime(2024, 1, 5, 0, 0, tzinfo=dt.UTC)


def test_currency_values_cast_with_indian_grouping():
    p = plan(["₹1,23,456.50", "₹2,000", "Rs. 300", "(₹50)"])
    assert p.pg_type == "numeric" and p.currency_ratio == 1.0
    assert cast(["₹1,23,456.50", "₹2,000", "Rs. 300", "(₹50)"], p).series.to_list() == [123456.5, 2000.0, 300.0, -50.0]


def test_european_decimal_comma_is_not_mistaken_for_grouping():
    assert plan(["1,5", "2,75"]).pg_type == "text"


def test_bool_cast_and_json_plan():
    assert cast(["yes", "no", "x"], inference.TypePlan("boolean")).series.to_list() == [True, False, None]
    assert plan(['{"a": 1}'], is_json=True).pg_type == "jsonb"


def test_parse_as_type_for_quality_rules():
    out = inference.parse_as_type(pl.Series(["2024-01-02", "bad", None]), "date")
    assert out.to_list() == [dt.date(2024, 1, 2), None, None]
    assert inference.parse_as_type(pl.Series(["5", "5.5"]), "bigint").to_list() == [5, None]


def test_infer_table_returns_named_typed_frame():
    frame = pl.DataFrame({"c0": ["1", "2"], "c1": ["a", "b"]})
    typed, cols = inference.infer_table(frame, ["id", "name"], ["ID", "Name"])
    assert typed.columns == ["id", "name"]
    assert [c.plan.pg_type for c in cols] == ["bigint", "text"]
    assert cols[0].source_name == "ID"


def test_sample_values_head_plus_reservoir():
    s = pl.Series([str(i) for i in range(500)])
    sample = inference.sample_values(s, head=100, random=50)
    assert sample.len() == 150 and sample.head(100).to_list() == [str(i) for i in range(100)]


def test_schema_hash_is_order_sensitive():
    assert inference.schema_hash([("a", "text"), ("b", "bigint")]) != inference.schema_hash([("b", "bigint"), ("a", "text")])


# --- column names -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,expected",
    [
        ("Customer ID", "customer_id"),
        ("customerID", "customer_id"),
        ("HTTPStatusCode", "http_status_code"),
        ("  Amount (₹) ", "amount_inr"),
        ("Growth %", "growth_pct"),
        ("Café Crème", "cafe_creme"),
        ("a--b__c", "a_b_c"),
        ("नाम", ""),
    ],
)
def test_to_snake(label, expected):
    assert to_snake(label) == expected


def test_normalize_column_names_unique_safe_and_bounded():
    names = normalize_column_names(["Name", "name", "NAME", "", "1st Place", "xmin", "x" * 80, "x" * 80, "नाम"])
    assert names[:3] == ["name", "name_2", "name_3"]
    assert names[3] == "column_4"
    assert names[4] == "c_1st_place"
    assert names[5] == "xmin_"
    assert len(names[6]) == 63 and len(names[7]) <= 63 and names[6] != names[7]
    assert names[8] == "column_9"
    assert len(set(names)) == len(names)
