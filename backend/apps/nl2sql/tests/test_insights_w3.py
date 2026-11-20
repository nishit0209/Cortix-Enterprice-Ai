"""W3 local insights: exact expected texts for the common chart shapes (synthetic numbers, no LLM)."""

from __future__ import annotations

from datetime import date

import pytest

from apps.nl2sql.insights import (
    bucket_label,
    compute_insights,
    detect_grain,
    human_measure,
    is_additive,
    narrate_template,
)

TODAY = date(2026, 11, 17)


def texts(ins):
    return [(i["kind"], i["text"]) for i in ins]


def test_top_contributors_with_shares():
    rows = [["North", 500.0], ["South", 300.0], ["East", 150.0], ["West", 50.0]]
    assert texts(compute_insights(["region", "total_revenue"], rows, today=TODAY)) == [
        ("top_n", "Top region by total revenue: North (500, 50.0%), South (300, 30.0%), East (150, 15.0%)."),
        ("share", "North accounts for 50.0% of the total revenue (1,000). The top 3 together make up 95.0%."),
    ]


def test_share_of_total_builder_shape_uses_the_value_column_not_the_pct():
    rows = [["North", 500.0, 50.0], ["South", 300.0, 30.0], ["East", 200.0, 20.0]]
    ins = compute_insights(["region", "total_revenue", "share_pct"], rows, today=TODAY)
    assert texts(ins) == [
        ("top_n", "Top region by total revenue: North (500, 50.0%), South (300, 30.0%), East (200, 20.0%)."),
        ("share", "North accounts for 50.0% of the total revenue (1,000)."),
    ]
    assert ins[1]["data"]["share"] == pytest.approx(0.5)


def test_averages_are_ranked_but_never_shared():
    ins = compute_insights(["category", "avg_discount"], [["A", 0.1], ["B", 0.2], ["C", 0.15]], today=TODAY)
    assert texts(ins) == [("top_n", "Top category by average discount: B (0.20), C (0.15), A (0.10).")]


def test_compare_periods_long_shape():
    rows = [["current_period", 1250.0], ["previous_period", 1000.0]]
    [ins] = compute_insights(["period", "total_revenue"], rows, today=TODAY)
    assert ins["text"] == "Total revenue rose 25.0% from 1,000 in the previous period to 1,250 in the current period."
    assert ins["data"]["change_pct"] == pytest.approx(0.25)


def test_compare_growth_wide_shape():
    [ins] = compute_insights(["current_period", "previous_period", "change_pct"], [[800.0, 1000.0, -20.0]],
                             today=TODAY)
    assert ins["text"] == "The value fell 20.0% from 1,000 in the previous period to 800 in the current period."


def test_monthly_trend_with_percent_change_and_in_progress_month():
    rows = [[date(2026, m, 1), 1000.0 + 100 * m] for m in range(1, 11)] + [[date(2026, 11, 1), 600.0]]
    ins = compute_insights(["month", "total_revenue"], rows, today=TODAY)
    assert texts(ins) == [
        ("period_change", "Total revenue rose 5.3% from 1,900 in Sep 2026 to 2,000 in Oct 2026. "
                          "Nov 2026 is still in progress (600 so far)."),
        ("trend", "Total revenue shows a steady upward trend over 10 periods (about 100 per period, 6.5% of the "
                  "average; R² 1.00). Overall it rose 81.8% from Jan 2026 to Oct 2026."),
    ]
    trend = ins[1]["data"]
    assert trend["grain"] == "month" and trend["points"] == 10
    assert trend["overall_change_pct"] == pytest.approx(900 / 1100)


def test_quarterly_downward_trend_counts():
    rows = [[date(2025, 1, 1), 400], [date(2025, 4, 1), 380], [date(2025, 7, 1), 350], [date(2025, 10, 1), 300],
            [date(2026, 1, 1), 280]]
    assert texts(compute_insights(["quarter", "distinct_order_id"], rows, today=TODAY)) == [
        ("period_change", "Number of orders fell 6.7% from 300 in Q4 2025 to 280 in Q1 2026."),
        ("trend", "Number of orders shows a steady downward trend over 5 periods (about 32.00 per period, 9.4% of "
                  "the average; R² 0.98). Overall it fell 30.0% from Q1 2025 to Q1 2026."),
    ]


def test_daily_anomaly_by_robust_z_score():
    values = [100, 104, 98, 101, 99, 350, 102, 97, 100, 103]
    rows = [[date(2026, 10, d), v] for d, v in zip(range(1, 11), values, strict=True)]
    ins = compute_insights(["day", "total_revenue"], rows, today=TODAY)
    assert texts(ins)[-1] == ("anomaly", "2026-10-06 is unusually high at 350 (typical value 100.50).")
    assert ins[-1]["data"]["z"] >= 3


def test_growth_series_largest_moves():
    rows = [[date(2026, 1, 1), 100.0, None], [date(2026, 2, 1), 125.0, 25.0], [date(2026, 3, 1), 110.0, -12.0],
            [date(2026, 4, 1), 121.0, 10.0]]
    ins = compute_insights(["month", "total_revenue", "growth_pct"], rows, today=TODAY)
    assert ("growth", "The largest increase was 25.0% in Feb 2026; the largest drop was 12.0% in Mar 2026.") in texts(ins)
    assert ins[0]["text"] == "Total revenue rose 10.0% from 110 in Mar 2026 to 121 in Apr 2026."


def test_weekly_in_progress_week():
    rows = [[date(2026, 10, 19), 10.0], [date(2026, 10, 26), 12.0], [date(2026, 11, 2), 9.0],
            [date(2026, 11, 9), 15.0], [date(2026, 11, 16), 4.0]]
    ins = compute_insights(["week", "total_revenue"], rows, today=TODAY)
    assert ins[0]["text"] == ("Total revenue rose 66.7% from 9 in week of 2026-11-02 to 15 in week of 2026-11-09. "
                              "Week of 2026-11-16 is still in progress (4 so far).")


@pytest.mark.parametrize(("cols", "row", "text"), [
    (["total_revenue"], [1234567.891], "Total revenue is 1.23M."),
    (["distinct_order_id"], [5000], "Number of orders is 5,000."),
    (["row_count"], [42], "Number of rows is 42."),
    (["revenue_per_order"], [2468.5], "Revenue per order is 2,468.50."),
])
def test_single_value_summary(cols, row, text):
    [ins] = compute_insights(cols, [row], today=TODAY)
    assert ins == {"kind": "summary", "text": text, "data": {"measure": cols[0], "value": float(row[0])}}


def test_narration_orders_kinds_and_keeps_numbers():
    rows = [[date(2026, m, 1), 1000.0 + 100 * m] for m in range(1, 11)] + [[date(2026, 11, 1), 600.0]]
    text = narrate_template("monthly revenue", compute_insights(["month", "total_revenue"], rows, today=TODAY))
    assert text.startswith("Total revenue rose 5.3% from 1,900 in Sep 2026 to 2,000 in Oct 2026.")
    assert "Overall it rose 81.8%" in text


def test_decimal_values_from_the_database():
    from decimal import Decimal

    ins = compute_insights(["channel", "total_revenue"], [["Online", Decimal("600.50")], ["Retail", Decimal("400.50")]],
                           today=TODAY)
    assert ins[0]["text"] == "Top channel by total revenue: Online (600.50, 60.0%), Retail (400.50, 40.0%)."


@pytest.mark.parametrize(("name", "human"), [
    ("total_revenue", "total revenue"), ("avg_discount", "average discount"), ("distinct_customer_id",
                                                                               "number of customers"),
    ("max_unit_price", "maximum unit price"), ("revenue", "revenue"), ("row_count", "number of rows"),
])
def test_human_measure(name, human):
    assert human_measure(name) == human


def test_additivity_and_grains():
    assert is_additive("total_revenue") and is_additive("distinct_order_id")
    assert not is_additive("avg_discount") and not is_additive("revenue_per_order") and not is_additive("share_pct")
    import pandas as pd

    months = [pd.Timestamp(2026, m, 1) for m in (1, 2, 3)]
    assert detect_grain(months) == "month"
    assert detect_grain([pd.Timestamp(2026, 1, 5), pd.Timestamp(2026, 1, 12)]) == "week"
    assert detect_grain([pd.Timestamp(2025, 1, 1), pd.Timestamp(2026, 1, 1)]) == "year"
    assert bucket_label(pd.Timestamp(2026, 7, 1), "quarter") == "Q3 2026"
    assert bucket_label(pd.Timestamp(2026, 7, 1), "year") == "2026"
