"""Semantic types, PII detection, profiling and the analyze() pipeline (no DB)."""

import datetime as dt

import polars as pl
import pytest

from apps.datasets.ingest import profiling, semantics
from apps.datasets.ingest.analyze import analyze
from apps.datasets.ingest.inference import TypePlan
from apps.datasets.ingest.parsing import ParsedTable


def sem(name, values, pg="text", **plan_kw):
    series = pl.Series(values)
    return semantics.detect_semantic(name, TypePlan(pg, **plan_kw), series, [str(v) for v in values if v is not None])


# --- semantic types ----------------------------------------------------------------------------------
def test_semantic_email_url_phone_uuid():
    assert sem("contact", [f"user{i}@example.test" for i in range(10)]) == "email"
    assert sem("site", [f"https://example.test/{i}" for i in range(10)]) == "url"
    assert sem("mobile", [f"+91 98765 4321{i}" for i in range(10)]) == "phone"
    assert sem("ref", ["0b2f8c0e-1c9a-4a8e-9a37-2a4f2b6f0c1d", "1b2f8c0e-1c9a-4a8e-9a37-2a4f2b6f0c1d"]) == "id"


def test_semantic_geo():
    assert sem("country", ["India", "Japan", "India", "United States"]) == "geo_country"
    assert sem("country_code", ["IN", "US", "GB", "IN"]) == "geo_country"
    assert sem("location", ["Mumbai", "Pune", "Delhi", "Chennai"]) == "geo_city"


def test_semantic_numeric_kinds():
    assert sem("amount", [1.5, 2.5], "numeric", currency_ratio=1.0) == "currency"
    assert sem("growth", [1.5, 2.5], "double precision", percent_ratio=1.0) == "percent"
    assert sem("order_id", [1, 2, 3, 4], "bigint") == "id"
    assert sem("quantity", [5, 1, 5, 2], "bigint") == "number"
    assert sem("price", [5.5, 1.25], "double precision") == "currency"


def test_semantic_category_free_text_and_dates():
    assert sem("segment", ["gold", "silver", "gold", "bronze"] * 30) == "category"
    long_text = [f"This is a fairly long synthetic free text comment number {i} about the order." for i in range(20)]
    assert sem("comment", long_text) == "free_text"
    assert sem("created", [dt.date(2024, 1, 1)], "date") == "date"
    assert sem("flag", [True, False], "boolean") == "boolean"


def test_is_category_rules():
    assert semantics.is_category(3, 1000)
    assert not semantics.is_category(1001, 100000)
    assert semantics.is_category(4, 40)
    assert not semantics.is_category(40, 40)


# --- PII ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [("email", "email"), ("customer_email", "email"), ("mobile_number", "phone"), ("first_name", "person_name"),
     ("name", "person_name"), ("product_name", None), ("pan_number", "national_id"), ("dob", "date_of_birth"),
     ("shipping_address", "address"), ("ip_address", "ip_address"), ("amount", None)],
)
def test_pii_from_name(name, expected):
    assert semantics.pii_from_name(name) == expected


def test_pii_from_values():
    assert semantics.pii_from_values([f"u{i}@example.test" for i in range(10)])[0] == "email"
    assert semantics.pii_from_values(["ABCDE1234F", "PQRSX9876Z"])[0] == "national_id"
    assert semantics.pii_from_values(["2345 6789 0123", "3456 7890 1234"])[0] == "national_id"
    assert semantics.pii_from_values(["4111 1111 1111 1111", "5500-0000-0000-0004"])[0] == "financial"
    assert semantics.pii_from_values(["10.0.0.1", "192.168.1.20"])[0] == "ip_address"
    assert semantics.pii_from_values(["+91 98765 43210", "+1 415 555 0100"])[0] == "phone"
    assert semantics.pii_from_values(["9876543210", "9123456789"])[0] is None  # bare digits need a name hint
    assert semantics.pii_from_values(["9876543210", "9123456789"], name_hint="phone")[0] == "phone"
    assert semantics.pii_from_values(["red", "blue"])[0] is None


def test_detect_pii_combines_name_and_values():
    r = semantics.detect_pii("contact", [f"user{i}@example.test" for i in range(5)])
    assert r.is_pii and r.pii_type == "email" and r.source == "values"
    r = semantics.detect_pii("first_name", ["Asha", "Ravi"])
    assert r.is_pii and r.pii_type == "person_name" and r.source == "name"
    assert not semantics.detect_pii("city", ["Pune", "Delhi"]).is_pii


# --- profiling ----------------------------------------------------------------------------------------
def test_numeric_profile_and_histogram():
    s = pl.Series("v", [float(i) for i in range(1, 101)] + [None])
    stats = profiling.profile_column(s, "double precision")
    assert stats["null_count"] == 1 and stats["null_pct"] == pytest.approx(0.99, abs=0.01)
    assert stats["distinct"] == 100 and stats["min"] == 1.0 and stats["max"] == 100.0
    assert stats["mean"] == pytest.approx(50.5) and stats["p50"] == pytest.approx(50.5)
    assert len(stats["histogram"]) == 20 and sum(b["count"] for b in stats["histogram"]) == 100
    assert stats["top_values"][0]["count"] == 1


def test_constant_column_histogram_single_bin():
    assert profiling.histogram(pl.Series([5, 5, 5])) == [{"start": 5.0, "end": 5.0, "count": 3}]


def test_top_values_suppressed_for_pii_and_text_lengths():
    s = pl.Series("e", ["a@example.test", "b@example.test", "a@example.test"])
    stats = profiling.profile_column(s, "text", is_pii=True)
    assert stats["top_values"] is None and stats["min"] is None
    assert stats["min_length"] == 14
    stats2 = profiling.profile_column(pl.Series("c", ["x", "y", "x"]), "text")
    assert stats2["top_values"][0] == {"value": "x", "count": 2}


def test_temporal_and_boolean_profiles():
    d = pl.Series("d", [dt.date(2024, 1, i) for i in range(1, 11)])
    stats = profiling.profile_column(d, "date")
    assert stats["min"] == "2024-01-01" and stats["max"] == "2024-01-10" and stats["histogram"]
    b = profiling.profile_column(pl.Series("b", [True, False, True, None]), "boolean")
    assert b["true_count"] == 2 and b["false_count"] == 1


def test_dataset_profile_keys_duplicates_and_correlation():
    frame = pl.DataFrame({
        "id": [1, 2, 3, 4, 4],
        "a": [3.0, 1.0, 4.0, 1.5, 1.5],
        "b": [6.0, 2.0, 8.0, 3.0, 3.0],
        "code": ["A", "B", "C", "D", "D"],
    })
    cols = [("id", "bigint"), ("a", "double precision"), ("b", "double precision"), ("code", "text")]
    prof = profiling.profile_dataset(frame, cols)
    assert prof["duplicate_rows"] == 1
    assert prof["candidate_keys"] == []
    assert prof["correlated_pairs"] == [{"a": "a", "b": "b", "r": 1.0}]
    unique_frame = frame.head(4)
    assert profiling.candidate_keys(unique_frame, cols) == ["id", "code"]


def test_schema_drift():
    drift = profiling.schema_drift([("a", "text"), ("b", "bigint")], [("b", "text"), ("c", "date")])
    assert drift == {"added": ["c"], "removed": ["a"], "retyped": [{"column": "b", "from": "bigint", "to": "text"}]}


# --- analyze ----------------------------------------------------------------------------------------
def test_analyze_end_to_end():
    frame = pl.DataFrame({
        "c0": [str(1000 + i) for i in range(30)],
        "c1": [f"person{i}@example.test" for i in range(30)],
        "c2": [["Mumbai", "Pune", "Delhi"][i % 3] for i in range(30)],
        "c3": [f"₹{i * 100:,}" for i in range(30)],
        "c4": [f"2024-01-{i % 28 + 1:02d}" for i in range(30)],
    })
    parsed = ParsedTable(frame=frame, source_names=["Customer ID", "E-mail", "City", "Spend (₹)", "Joined"])
    out = analyze(parsed)
    by = {c.name: c for c in out.columns}
    assert list(by) == ["customer_id", "e_mail", "city", "spend_inr", "joined"]
    assert by["customer_id"].data_type == "bigint" and by["customer_id"].semantic_type == "id"
    assert by["e_mail"].is_pii and by["e_mail"].pii_type == "email" and by["e_mail"].stats["top_values"] is None
    assert by["city"].semantic_type == "geo_city" and not by["city"].is_pii
    assert by["spend_inr"].data_type == "numeric" and by["spend_inr"].semantic_type == "currency"
    assert by["joined"].data_type == "date"
    assert out.profile["row_count"] == 30 and "customer_id" in out.profile["candidate_keys"]
    assert out.profile["pii_columns"] == ["e_mail"]
    assert len(out.schema_hash) == 64
