"""Pure result helpers: JSON values, caps, chart heuristic, formula-injection-safe export cells."""

from __future__ import annotations

import io
import math
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apps.bi import results
from apps.bi.services import _csv_stream, _xlsx_bytes
from apps.bi.tests.fakes import DANGEROUS_RESULT
from nexora.contracts import ChartHint, ColumnMeta, ResultSet
from testing.security import unescaped_formula_cells


def cols(*spec):
    return [{"name": n, "type": t, "masked": m} for n, t, m in spec]


# ------------------------------------------------------------------------------------------------ jsonable
def test_jsonable_converts_database_values():
    u = uuid.uuid4()
    assert results.jsonable(Decimal("12.50")) == 12.5
    assert results.jsonable(Decimal("12")) == 12 and isinstance(results.jsonable(Decimal("12")), int)
    assert results.jsonable(Decimal("NaN")) is None
    assert results.jsonable(float("inf")) is None
    assert results.jsonable(date(2026, 1, 2)) == "2026-01-02"
    assert results.jsonable(datetime(2026, 1, 2, 3, 4, tzinfo=UTC)) == "2026-01-02T03:04:00+00:00"
    assert results.jsonable(timedelta(minutes=2)) == 120.0
    assert results.jsonable(u) == str(u)
    assert results.jsonable(b"\x00\x01") == "AAE="
    assert results.jsonable({"a": [Decimal("1.5"), None, True]}) == {"a": [1.5, None, True]}


def test_serialize_result_caps_rows_and_bytes():
    rs = ResultSet(columns=(ColumnMeta("n", "bigint"), ColumnMeta("s", "text", masked=True)),
                   rows=tuple((i, "x" * 50) for i in range(30)), row_count=30, truncated=False, duration_ms=1)
    full = results.serialize_result(rs)
    assert full["row_count"] == 30 and len(full["rows"]) == 30 and full["truncated"] is False
    assert full["columns"][1] == {"name": "s", "type": "text", "masked": True}
    by_rows = results.serialize_result(rs, max_rows=10)
    assert len(by_rows["rows"]) == 10 and by_rows["truncated"] is True and by_rows["row_count"] == 30
    by_bytes = results.serialize_result(rs, max_bytes=300)
    assert 1 <= len(by_bytes["rows"]) < 10 and by_bytes["truncated"] is True
    executor_cut = ResultSet(columns=rs.columns, rows=rs.rows[:2], row_count=2, truncated=True, duration_ms=1)
    assert results.serialize_result(executor_cut)["truncated"] is True


# ------------------------------------------------------------------------------------------------ charts
def test_chart_time_series_is_a_line():
    c = cols(("month", "date", False), ("revenue", "numeric", False))
    rows = [["2026-01-01", 10], ["2026-02-01", 12], ["2026-03-01", 9]]
    assert results.choose_chart(None, c, rows) == {"type": "line", "x": "month", "y": ["revenue"], "series": None}


def test_chart_time_series_with_a_category_uses_series():
    c = cols(("month", "date", False), ("region", "text", False), ("revenue", "numeric", False))
    rows = [["2026-01-01", "West", 10], ["2026-01-01", "East", 4]]
    assert results.choose_chart(None, c, rows) == {"type": "line", "x": "month", "y": ["revenue"], "series": "region"}


def test_chart_category_is_a_bar_and_small_share_is_a_pie():
    c = cols(("region", "text", False), ("revenue", "numeric", False))
    many = [[f"r{i}", i * 10] for i in range(8)]
    assert results.choose_chart(None, c, many)["type"] == "bar"
    few = [["West", 50], ["East", 30], ["North", 20]]  # sums to 100
    assert results.choose_chart(None, c, few)["type"] == "pie"
    share = cols(("region", "text", False), ("revenue_share", "numeric", False))
    assert results.choose_chart(None, share, [["West", 7], ["East", 3]])["type"] == "pie"
    not_share = [["West", 500], ["East", -30]]
    assert results.choose_chart(None, c, not_share)["type"] == "bar"
    assert results.choose_chart(None, c, [["West", 5], ["East", 9]], question="share of revenue by region")["type"] == "pie"


def test_chart_single_value_is_a_kpi_and_others_are_tables():
    assert results.choose_chart(None, cols(("total", "numeric", False)), [[42]])["type"] == "kpi"
    assert results.choose_chart(None, cols(("name", "text", False), ("email", "text", False)),
                                [["a", "b"]])["type"] == "table"
    assert results.choose_chart(None, [], []) is None
    assert results.choose_chart(None, cols(("x", "text", False)), []) == {"type": "table", "x": None, "y": [],
                                                                          "series": None}


def test_chart_types_are_inferred_from_values_when_types_are_generic():
    c = cols(("day", "unknown", False), ("n", "unknown", False))
    rows = [[date(2026, 1, 1), 3], [date(2026, 1, 2), 4]]
    assert results.choose_chart(None, c, rows)["type"] == "line"


def test_chart_hint_is_used_only_when_it_matches_the_result():
    c = cols(("region", "text", False), ("revenue", "numeric", False), ("email", "text", True))
    rows = [["West", 1, "h1"], ["East", 2, "h2"]]
    hint = ChartHint(type="area", x="region", y=("revenue",))
    assert results.choose_chart(hint, c, rows) == {"type": "area", "x": "region", "y": ["revenue"], "series": None}
    assert results.choose_chart(ChartHint(type="bar", x="nope", y=("revenue",)), c, rows)["x"] == "region"
    assert results.choose_chart(ChartHint(type="bar", x="region", y=("email",)), c, rows)["y"] == ["revenue"]  # masked y
    assert results.choose_chart({"type": "kpi", "y": ["revenue"]}, c, rows)["type"] == "kpi"
    assert results.choose_chart({"type": "radar", "x": "region", "y": ["revenue"]}, c, rows)["type"] == "bar"


def test_masked_numeric_columns_are_never_plotted():
    c = cols(("region", "text", False), ("salary", "numeric", True))
    assert results.choose_chart(None, c, [["West", 1], ["East", 2]])["type"] == "table"


def test_chart_hint_round_trip():
    assert results.chart_hint_from_dict({"type": "bar", "x": "a", "y": ["b"]}) == ChartHint("bar", "a", ("b",))
    assert results.chart_hint_from_dict({"type": "radar"}) is None
    assert results.chart_hint_from_dict(None) is None


# ------------------------------------------------------------------------------------------------ exports
@pytest.mark.parametrize("value", ["=1+1", "+1", "-A1", "@SUM(A1)", "\tx", "\rx"])
def test_formula_triggers_are_prefixed(value):
    assert results.escape_formula(value) == "'" + value
    assert results.csv_cell(value) == "'" + value


def test_numbers_stay_numbers():
    assert results.csv_cell(-12.5) == -12.5
    assert results.csv_cell(Decimal("-3")) == -3
    assert results.csv_cell(None) == ""
    assert results.csv_cell(True) == "true"
    assert results.xlsx_cell(Decimal("-3.25")) == -3.25
    assert results.xlsx_cell("=cmd") == "'=cmd"
    assert results.xlsx_cell("a\x00b\x07c") == "abc"
    aware = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
    assert results.xlsx_cell(aware) == datetime(2026, 1, 2, 10, 0)


def test_csv_export_has_no_evaluable_cells():
    text = "".join(_csv_stream(["label", "=evil_header", "day"], DANGEROUS_RESULT.rows))
    assert text.startswith("﻿")
    assert unescaped_formula_cells(text.lstrip("﻿")) == []
    assert "'=HYPERLINK" in text and "'+cmd" in text and "'@SUM" in text and "'\tTabbed" in text
    assert "'-2+3" in text
    assert ",-12.5," in text  # numeric negatives are not formulas and stay numeric
    assert "'=evil_header" in text


def test_xlsx_export_has_no_formulas():
    from openpyxl import load_workbook

    data = _xlsx_bytes(["label", "value", "day"], DANGEROUS_RESULT.rows)
    ws = load_workbook(io.BytesIO(data)).active
    values = [[cell.value for cell in row] for row in ws.iter_rows()]
    assert values[0] == ["label", "value", "day"]
    for row in ws.iter_rows(min_row=2):
        label = row[0]
        assert label.data_type == "s"  # never 'f' (formula)
        assert not str(label.value).startswith(("=", "+", "@", "\t")) or str(label.value).startswith("'")
    assert values[1][0].startswith("'=HYPERLINK")
    assert values[1][1] == -12.5 and values[2][1] == 3
    assert values[1][2].date() == date(2026, 1, 5)
    assert math.isclose(values[4][1], 1.5)


@pytest.mark.parametrize("chart", [
    {"type": ["bar"]}, {"type": {"bar": 1}}, {"type": None}, {"type": 3}, {"type": "radar"}, {}, ["bar"], "bar",
])
def test_clean_chart_rejects_bad_types_with_a_validation_error(chart):
    from rest_framework.exceptions import ValidationError

    from apps.bi.dashboards import clean_chart

    with pytest.raises(ValidationError):  # never a TypeError (unhashable list/dict) -> HTTP 500
        clean_chart(chart)


def test_clean_chart_normalises_a_valid_chart():
    from apps.bi.dashboards import clean_chart

    assert clean_chart(None) is None
    assert clean_chart({"type": "bar", "x": "region", "y": "revenue"}) == {
        "type": "bar", "x": "region", "y": ["revenue"], "series": None}
