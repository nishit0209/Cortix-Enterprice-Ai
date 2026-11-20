"""Local insights + templated narration with exact numbers (synthetic data)."""

from __future__ import annotations

from datetime import date

import pytest

from apps.nl2sql.insights import compute_insights, fmt_num, narrate_template


def kinds(ins):
    return [i["kind"] for i in ins]


def test_top_n_and_share():
    cols = ["region", "total_amount"]
    rows = [["North", 500.0], ["South", 300.0], ["East", 150.0], ["West", 50.0]]
    ins = compute_insights(cols, rows, complete=True)  # every region is in the rows
    assert kinds(ins)[:2] == ["top_n", "share"]
    top = ins[0]["data"]["items"]
    assert [i["label"] for i in top] == ["North", "South", "East"]
    share = ins[1]["data"]
    assert share["label"] == "North" and share["share"] == pytest.approx(0.5) and share["total"] == 1000.0
    assert share["top_n_share"] == pytest.approx(0.95)
    assert ins[1]["text"] == ("North accounts for 50.0% of the total amount (1,000). "
                              "The top 3 together make up 95.0%.")
    assert ins[0]["text"] == "Top region by total amount: North (500, 50.0%), South (300, 30.0%), East (150, 15.0%)."


def test_period_change_compare_shape():
    cols = ["period", "total_amount"]
    rows = [["last_month", 1000.0], ["this_month", 1250.0]]
    [ins] = compute_insights(cols, rows)
    assert ins["kind"] == "period_change"
    d = ins["data"]
    assert d["current"] == 1250.0 and d["previous"] == 1000.0
    assert d["change_pct"] == pytest.approx(0.25) and d["direction"] == "up"
    assert ins["text"] == "Total amount rose 25.0% from 1,000 in last month to 1,250 in this month."


def test_wide_compare_and_zero_previous():
    [ins] = compute_insights(["this_month", "last_month"], [[80, 100]])
    assert ins["data"]["change_pct"] == pytest.approx(-0.2) and "fell 20.0%" in ins["text"]
    [z] = compute_insights(["this_month", "last_month"], [[80, 0]])
    assert z["data"]["change_pct"] is None and "versus 0" in z["text"]


def test_time_series_trend_and_period_change():
    cols = ["month", "revenue"]
    rows = [[date(2026, m, 1), 100.0 + 10 * m] for m in range(1, 9)]
    ins = compute_insights(cols, rows)
    assert kinds(ins)[:2] == ["period_change", "trend"]
    pc, tr = ins[0]["data"], ins[1]["data"]
    assert pc["current"] == 180.0 and pc["previous"] == 170.0
    assert pc["change_pct"] == pytest.approx(10 / 170)
    assert tr["slope_per_period"] == pytest.approx(10.0) and tr["r2"] == pytest.approx(1.0)
    assert tr["direction"] == "upward"
    assert "Aug 2026" in ins[0]["text"] and "Jul 2026" in ins[0]["text"]


def test_no_trend_for_noise():
    rows = [[f"2026-0{m}-01", v] for m, v in zip(range(1, 9), [5, 9, 4, 8, 5, 9, 4, 8], strict=True)]
    ins = compute_insights(["month", "revenue"], rows)
    trend = next(i for i in ins if i["kind"] == "trend")
    assert trend["data"]["direction"] == "none"


def test_anomaly_detection():
    rows = [[f"store_{i}", v] for i, v in enumerate([100, 102, 98, 101, 99, 100, 103, 97, 400])]
    ins = compute_insights(["store", "sales"], rows)
    anomalies = [i for i in ins if i["kind"] == "anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0]["data"]["label"] == "store_8" and anomalies[0]["data"]["value"] == 400
    assert "unusually high at 400 (typical value 100)" in anomalies[0]["text"]


def test_masked_columns_are_ignored_and_empty_results():
    cols = [{"name": "customer_email", "type": "text", "masked": True}, {"name": "amount", "type": "numeric"}]
    ins = compute_insights(cols, [["x", 5], ["y", 7]])
    assert all("customer_email" not in str(i) for i in ins)
    assert compute_insights(["a"], []) == []
    assert compute_insights(["a"], [["text only"]]) == []


def test_narrate_template_uses_real_numbers():
    ins = compute_insights(["period", "total_amount"], [["last_month", 1000.0], ["this_month", 1250.0]])
    text = narrate_template("compare revenue this month vs last month", ins)
    assert "25.0%" in text and "1,000" in text and "1,250" in text
    assert narrate_template("q", [], row_count=0) == "The query returned no rows for this question."
    assert narrate_template("q", [], row_count=1) == "The query returned 1 row."


@pytest.mark.parametrize(("v", "s"), [(1234567, "1.23M"), (2.5e9, "2.50B"), (12345, "12,345"), (42, "42"),
                                      (3.14159, "3.14"), (None, "n/a")])
def test_fmt_num(v, s):
    assert fmt_num(v) == s


def test_narrate_with_gemini_is_gated(monkeypatch, settings):
    from uuid import uuid4

    from apps.ai.policy import check_policy
    from apps.ai.services import override_gemini
    from apps.ai.testing import build_engine
    from apps.core.exceptions import AIPolicyBlocked
    from apps.core.tenancy.context import current_org
    from apps.nl2sql.insights import narrate, narrate_with_gemini

    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    token = current_org.set(uuid4())
    try:
        def schema_only(payload):
            return check_policy(payload, state={"policy": "schema_only", "consent_valid": True},
                                classify=lambda r: "internal", tier="free")

        engine, p = build_engine(policy_check=schema_only)
        with override_gemini(engine):
            with pytest.raises(AIPolicyBlocked):
                narrate_with_gemini("q", ["region", "v"], [["N", 1]], [], dataset_id=uuid4())
        assert p["transport"].total_calls == 0
        # narrate() falls back to the local template when VALUES are not allowed
        import apps.ai.services as ai_services

        monkeypatch.setattr(ai_services, "policy_allows", lambda payload: False)
        with override_gemini(engine):
            text = "".join(narrate("q", ["region", "v"], [["N", 3], ["S", 1]], dataset_id=uuid4(),
                                   prefer_gemini=True))
        assert "N" in text and p["transport"].total_calls == 0
    finally:
        current_org.reset(token)
