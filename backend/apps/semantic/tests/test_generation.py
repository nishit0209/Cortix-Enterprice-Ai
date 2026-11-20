"""Pure tests of semantic-layer generation (roles, names, synonyms, descriptions, default metrics). No DB."""

from __future__ import annotations

import pytest

from apps.datasets.naming import is_safe_identifier, logical_table_name
from apps.semantic import generation as g
from apps.sqlguard.services import validate_expression


def col(name, data_type="text", semantic_type=None, is_pii=False, source_name=None, ordinal=0, pii_type=None):
    return {"name": name, "data_type": data_type, "semantic_type": semantic_type, "is_pii": is_pii,
            "source_name": source_name or name, "ordinal": ordinal, "pii_type": pii_type}


@pytest.mark.parametrize(
    "c, role",
    [
        (col("email", semantic_type="email", is_pii=True), "pii"),
        (col("order_id", "bigint", "id"), "id"),
        (col("customer_key", "text"), "id"),
        (col("id", "bigint"), "id"),
        (col("order_date", "date", "date"), "time"),
        (col("created_at", "timestamptz"), "time"),
        (col("revenue", "numeric", "currency"), "measure"),
        (col("quantity", "bigint", "number"), "measure"),
        (col("discount_pct", "double precision", "percent"), "measure"),
        (col("year", "bigint", "number"), "dimension"),
        (col("store_count_bucket", "bigint", "category"), "dimension"),
        (col("is_active", "boolean", "boolean"), "dimension"),
        (col("region", "text", "category"), "dimension"),
        (col("city", "text", "geo_city"), "dimension"),
    ],
)
def test_roles(c, role):
    assert g.column_spec(c).role == role


def test_default_aggregations():
    assert g.column_spec(col("revenue", "numeric", "currency")).default_agg == "sum"
    assert g.column_spec(col("unit_price", "numeric", "currency")).default_agg == "avg"
    assert g.column_spec(col("discount", "double precision", "percent")).default_agg == "avg"
    assert g.column_spec(col("order_id", "bigint", "id")).default_agg == "count_distinct"
    assert g.column_spec(col("region")).default_agg is None


def test_display_names_and_value_free_descriptions():
    spec = g.column_spec(col("lifetime_value", "numeric", "currency", source_name="Lifetime Value (₹)"))
    assert spec.display_name == "Lifetime Value"
    assert "numeric measure" in spec.description and "Lifetime Value (₹)" in spec.description
    pii = g.column_spec(col("phone", semantic_type="phone", is_pii=True, pii_type="phone"))
    assert "personal data" in pii.description and pii.synonyms == ()


def test_business_synonyms():
    syn = g.column_spec(col("total_revenue", "numeric", "currency")).synonyms
    assert "total revenue" in syn and "total sales" in syn and "total turnover" in syn
    assert len(syn) <= g.MAX_SYNONYMS
    assert "zone" in g.column_spec(col("region")).synonyms
    assert "client name" in g.column_spec(col("customer_name")).synonyms


def test_default_metrics_are_valid_guard_expressions():
    specs = [g.column_spec(c) for c in (
        col("order_id", "bigint", "id"), col("revenue", "numeric", "currency"), col("unit_price", "numeric", "currency"),
        col("order", "bigint", "number"), col("email", semantic_type="email", is_pii=True), col("region"),
    )]
    metrics = g.default_metrics("sales", specs, "Synthetic Sales")
    keys = [m["key"] for m in metrics]
    assert keys == ["row_count", "total_revenue", "avg_revenue", "avg_unit_price", "total_order", "avg_order"]
    names = [s.name for s in specs]
    for m in metrics:
        assert set(m) == {"key", "name", "expression", "description", "synonyms", "table"}
        validate_expression(m["expression"], names)
    assert next(m for m in metrics if m["key"] == "total_order")["expression"] == 'SUM("order")'
    assert "total sales" in next(m for m in metrics if m["key"] == "total_revenue")["synonyms"]


def test_schema_hash_is_stable_and_sensitive():
    a = g.schema_hash({"sales": ""}, {"sales": [("a", "text"), ("b", "bigint")]})
    assert a == g.schema_hash({"sales": ""}, {"sales": [("a", "text"), ("b", "bigint")]})
    assert a != g.schema_hash({"sales": ""}, {"sales": [("a", "text"), ("b", "numeric")]})


@pytest.mark.parametrize(
    "slug, logical",
    [
        ("sales", "sales"),
        ("sales-2024", "sales_2024"),
        ("2024-sales", "t_2024_sales"),
        ("pg-stats", "t_pg_stats"),
        ("select", "select_data"),
        ("user", "user_data"),
        ("customers--crm", "customers_crm"),
        ("", "dataset"),
        ("a" * 80, "a" * 63),
    ],
)
def test_logical_table_names(slug, logical):
    assert logical_table_name(slug) == logical
    assert is_safe_identifier(logical)


def test_metric_key():
    assert g.metric_key("Average Order Value") == "average_order_value"
    assert g.metric_key("2024 total") == "m_2024_total"
