"""W4: BI passes the EXECUTED sql and `truncated` to nl2sql.compute_insights (no DB)."""

from __future__ import annotations

from apps.bi import ports, services
from nexora.contracts import ColumnMeta, ResultSet


def _rs(truncated: bool) -> ResultSet:
    return ResultSet(columns=(ColumnMeta("region", "text"), ColumnMeta("revenue", "numeric")),
                     rows=(("North", 10), ("South", 30)), row_count=2, truncated=truncated, duration_ms=1)


def test_chat_insights_forward_the_executed_sql_and_truncated(monkeypatch):
    seen = {}

    def fake(columns, rows, **kwargs):
        seen.update(columns=columns, rows=rows, **kwargs)
        return [{"kind": "share", "text": "South is 75%", "data": {"pct": 75}}, {"kind": "x"}, "junk"]

    monkeypatch.setattr(ports, "compute_insights", fake)
    out = services._Chat._insights(object(), _rs(True), sql="SELECT region, sum(x) FROM t GROUP BY 1 LIMIT 2")
    assert seen["sql"].endswith("LIMIT 2") and seen["truncated"] is True
    assert [c["name"] for c in seen["columns"]] == ["region", "revenue"]
    assert seen["rows"] == [["North", 10], ["South", 30]]
    assert out == [{"kind": "share", "text": "South is 75%", "data": {"pct": 75}}]


def test_chat_insights_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(ports, "compute_insights", lambda *a, **k: 1 / 0)
    assert services._Chat._insights(object(), _rs(False), sql="SELECT 1") == []


def test_port_forwards_only_the_known_keywords(monkeypatch):
    from apps.nl2sql import services as nl2sql

    seen = {}
    monkeypatch.setattr(nl2sql, "compute_insights",
                        lambda columns, rows, **kw: seen.update(kw) or [{"kind": "k", "text": "t"}])
    assert ports.compute_insights([], [], sql="SELECT 1", truncated=False, complete=None, bogus=1) == [
        {"kind": "k", "text": "t"}]
    assert seen == {"sql": "SELECT 1", "truncated": False, "complete": None}


def test_port_against_the_real_nl2sql_never_claims_a_total_for_a_limited_result():
    cols = [{"name": "region", "type": "text"}, {"name": "revenue", "type": "numeric"}]
    rows = [["North", 10], ["South", 30]]
    limited = ports.compute_insights(cols, rows, sql="SELECT region, revenue FROM t ORDER BY 2 DESC LIMIT 2",
                                     truncated=False)
    full = ports.compute_insights(cols, rows, sql="SELECT region, sum(revenue) FROM t GROUP BY 1", truncated=False)
    assert isinstance(limited, list) and isinstance(full, list)
    assert not any("total" in str(i.get("text", "")).lower() for i in limited)
