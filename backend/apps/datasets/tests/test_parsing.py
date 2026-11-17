"""Parsing edge cases: encodings, delimiters, headers, Excel, JSON nesting, malformed files (no DB)."""

import datetime as dt
import json

import pytest

from apps.datasets.ingest import parsing
from apps.datasets.ingest.errors import IngestError
from apps.datasets.tests.helpers import xlsx_bytes


@pytest.fixture
def write(tmp_path):
    def _write(name: str, data: bytes) -> str:
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)

    return _write


def values(table, i):
    return table.frame[f"c{i}"].to_list()


# --- encodings --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "encoding,bom",
    [("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")],
)
def test_unicode_encodings_and_boms(write, encoding, bom):
    text = "name,city\nJosé,Zürich\nRenée,Mumbai\n"
    path = write("t.csv", bom + text.encode(encoding))
    t = parsing.parse_delimited(path)
    assert t.source_names == ["name", "city"]
    assert values(t, 0) == ["José", "Renée"]
    assert values(t, 1) == ["Zürich", "Mumbai"]


def test_legacy_single_byte_encoding_is_detected(write):
    rows = "\n".join(f"Café {i},Señor Pérez {i},Crème brûlée façade naïve {i}" for i in range(60))
    path = write("t.csv", ("dish,chef,desc\n" + rows + "\n").encode("cp1252"))
    t = parsing.parse_delimited(path)
    assert t.meta["encoding"] != "utf-8"
    assert values(t, 0)[0] == "Café 0"
    assert values(t, 1)[1] == "Señor Pérez 1"


def test_explicit_encoding_option(write):
    path = write("t.csv", "a,b\nÄ,ö\n".encode("latin-1"))
    t = parsing.parse_delimited(path, options=parsing.ParseOptions(encoding="latin-1"))
    assert values(t, 0) == ["Ä"]


def test_detect_encoding_helper():
    assert parsing.detect_encoding(b"\xef\xbb\xbfabc") == ("utf-8", 3)
    assert parsing.detect_encoding(b"plain ascii") == ("utf-8", 0)
    # a multi-byte character cut at the sample boundary is still UTF-8
    assert parsing.detect_encoding("ab€".encode()[:-1]) == ("utf-8", 0)


# --- delimiters & headers -----------------------------------------------------------------------------
@pytest.mark.parametrize("delim", [",", ";", "\t", "|"])
def test_delimiter_sniffing(write, delim):
    content = delim.join(["id", "name", "amount"]) + "\n" + "\n".join(
        delim.join([str(i), f"item {i}", f"{i}.5"]) for i in range(10)
    )
    t = parsing.parse_delimited(write("t.txt", content.encode()), "txt")
    assert t.meta["delimiter"] == delim
    assert t.source_names == ["id", "name", "amount"]
    assert t.row_count == 10


def test_delimiter_inside_quotes_does_not_confuse_sniffer(write):
    content = 'name;note\n"Smith, John";"a, b, c"\n"Doe, Jane";"x, y"\n'
    t = parsing.parse_delimited(write("t.csv", content.encode()))
    assert t.meta["delimiter"] == ";"
    assert values(t, 0) == ["Smith, John", "Doe, Jane"]


def test_quoted_newlines_and_escaped_quotes(write):
    content = 'id,comment\n1,"line one\nline two"\n2,"he said ""hi"""\n'
    t = parsing.parse_delimited(write("t.csv", content.encode()))
    assert values(t, 1) == ["line one\nline two", 'he said "hi"']


def test_tsv_kind_forces_tab(write):
    t = parsing.parse_delimited(write("t.tsv", b"a\tb,c\n1\t2,3\n"), "tsv")
    assert t.source_names == ["a", "b,c"]


def test_headerless_numeric_file_gets_generated_names(write):
    t = parsing.parse_delimited(write("t.csv", b"1,2.5,2024-01-01\n2,3.5,2024-01-02\n"))
    assert t.meta["has_header"] is False
    assert t.source_names == ["column_1", "column_2", "column_3"]
    assert t.row_count == 2


def test_has_header_option_overrides_sniffing(write):
    t = parsing.parse_delimited(write("t.csv", b"2023,2024\n1,2\n"), options=parsing.ParseOptions(has_header=True))
    assert t.source_names == ["2023", "2024"]
    assert t.row_count == 1


def test_short_rows_are_padded_and_trailing_empty_column_dropped(write):
    t = parsing.parse_delimited(write("t.csv", b"a,b,c,\n1,2,3,\n4,5,,\n6\n"))
    assert t.source_names == ["a", "b", "c"]
    assert values(t, 1) == ["2", "5", None]


def test_header_only_file_has_zero_rows(write):
    t = parsing.parse_delimited(write("t.csv", b"a,b,c\n"))
    assert t.row_count == 0 and t.source_names == ["a", "b", "c"]


def test_ragged_rows_are_malformed(write):
    with pytest.raises(IngestError) as exc:
        parsing.parse_delimited(write("t.csv", b"a,b\n1,2\n3,4,5,6\n"))
    assert exc.value.code == "malformed_file"


def test_whitespace_only_file_is_empty(write):
    with pytest.raises(IngestError) as exc:
        parsing.parse_delimited(write("t.csv", b"   \n\n  "))
    assert exc.value.code == "upload_empty"


def test_column_cap(write, settings):
    settings.DATASET_MAX_COLUMNS = 5
    with pytest.raises(IngestError) as exc:
        parsing.parse_delimited(write("t.csv", (",".join(f"c{i}" for i in range(6)) + "\n" + ",".join("1" * 6)).encode()))
    assert exc.value.code == "too_many_columns"


# --- Excel ------------------------------------------------------------------------------------------
def test_excel_first_non_empty_sheet_and_typed_cells(write):
    data = xlsx_bytes(
        [
            ["Region", "Units", "Price", "When", "Flag", "Day"],
            ["North", 10, 10.5, dt.datetime(2024, 1, 5, 10, 30), True, dt.date(2024, 2, 1)],
            ["South", 3, 2.0, dt.datetime(2024, 1, 6, 0, 0), False, None],
        ],
        empty_first_sheet=True,
    )
    t = parsing.parse_excel(write("b.xlsx", data))
    assert t.meta["sheet"] == "Data"
    assert t.source_names == ["Region", "Units", "Price", "When", "Flag", "Day"]
    assert values(t, 1) == ["10", "3"]
    assert values(t, 2) == ["10.5", "2"]
    assert values(t, 3) == ["2024-01-05 10:30:00", "2024-01-06"]
    assert values(t, 4) == ["true", "false"]
    assert values(t, 5)[0] == "2024-02-01"


def test_excel_sheet_option_and_missing_sheet(write):
    path = write("b.xlsx", xlsx_bytes([["a"], [1]], sheet="Numbers"))
    assert parsing.parse_excel(path, parsing.ParseOptions(sheet="Numbers")).row_count == 1
    with pytest.raises(IngestError) as exc:
        parsing.parse_excel(path, parsing.ParseOptions(sheet="Nope"))
    assert exc.value.code == "sheet_not_found"


def test_excel_merged_two_row_header_is_flattened(write):
    data = xlsx_bytes(
        [["Sales", None, "Region"], ["Q1", "Q2", None], [100, 200, "North"], [150, 250, "South"]], merge="A1:B1"
    )
    t = parsing.parse_excel(write("m.xlsx", data))
    assert t.source_names == ["Sales Q1", "Sales Q2", "Region"]
    assert t.row_count == 2


def test_excel_all_sheets_empty(write):
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(IngestError) as exc:
        parsing.parse_excel(write("e.xlsx", buf.getvalue()))
    assert exc.value.code == "upload_empty"


def test_corrupt_excel_is_malformed(write):
    with pytest.raises(IngestError) as exc:
        parsing.parse_excel(write("c.xlsx", b"PK\x03\x04" + b"\x00" * 100))
    assert exc.value.code == "malformed_file"


# --- JSON -------------------------------------------------------------------------------------------
def test_json_array_with_nesting_flattened_to_depth_3(write):
    records = [
        {"id": 1, "customer": {"name": "A", "address": {"city": "Pune", "geo": {"lat": 1.5}}}, "tags": ["x", "y"]},
        {"id": 2, "customer": {"name": "B", "address": {"city": "Delhi", "geo": {"lat": 2.5}}}, "extra": True},
    ]
    t = parsing.parse_json(write("a.json", json.dumps(records).encode()), "json")
    assert t.source_names == ["id", "customer__name", "customer__address__city", "customer__address__geo", "tags", "extra"]
    geo_pos = t.source_names.index("customer__address__geo")
    tags_pos = t.source_names.index("tags")
    assert t.json_columns == {geo_pos, tags_pos}
    assert json.loads(values(t, geo_pos)[0]) == {"lat": 1.5}
    assert values(t, t.source_names.index("extra")) == [None, "true"]
    assert values(t, 0) == ["1", "2"]


def test_ndjson_and_blank_lines(write):
    t = parsing.parse_json(write("a.ndjson", b'{"a": 1}\n\n{"a": 2, "b": null}\n'), "ndjson")
    assert t.source_names == ["a", "b"]
    assert values(t, 0) == ["1", "2"]


def test_json_object_wrapper_with_single_record_list(write):
    t = parsing.parse_json(write("a.json", b'{"meta": {"v": 1}, "data": [{"x": 1}, {"x": 2}]}'), "json")
    assert t.source_names == ["x"] and t.row_count == 2


def test_ndjson_content_in_json_file(write):
    t = parsing.parse_json(write("a.json", b'{"x": 1}\n{"x": 2}\n'), "json")
    assert t.row_count == 2


def test_json_scalars_become_value_column(write):
    t = parsing.parse_json(write("a.json", b"[1, 2.5, \"x\"]"), "json")
    assert t.source_names == ["value"] and values(t, 0) == ["1", "2.5", "x"]


@pytest.mark.parametrize(
    "name,data",
    [("a.json", b'[{"a": 1}, {"a": }]'), ("a.ndjson", b'{"a": 1}\n{"a": \n'), ("a.json", b"[1, 2")],
)
def test_malformed_json(write, name, data):
    with pytest.raises(IngestError) as exc:
        parsing.parse_json(write(name, data), "ndjson" if name.endswith("ndjson") else "json")
    assert exc.value.code == "malformed_file"


def test_empty_json_array(write):
    with pytest.raises(IngestError) as exc:
        parsing.parse_json(write("a.json", b"[]"), "json")
    assert exc.value.code == "upload_empty"


def test_flatten_record_depth_rule():
    flat = parsing.flatten_record({"a": {"b": {"c": {"d": 1}}}, "e": {}})
    assert flat == {"a__b__c": {"d": 1}, "e": None}


def test_parse_file_dispatch(write):
    assert parsing.parse_file(write("x.csv", b"a\n1\n"), "csv").row_count == 1
    with pytest.raises(IngestError):
        parsing.parse_file(write("x.bin", b"a"), "parquet")
