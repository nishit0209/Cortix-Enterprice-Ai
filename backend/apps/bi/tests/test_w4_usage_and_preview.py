"""W4: `GET /usage` (real DB aggregates) and the policy-preview sample query (`POST /datasets/{id}/policy-preview`
with `sample: true`, executed by apps.bi under the member's policy intersected with the caller's own).

Unit tests need no DB; the DB tests run as nexora_app (RLS). The semantic layer / guard / executor are the bi fakes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.bi import policy_sample, usage
from apps.bi.tests.fakes import DATASET_ID, SALES_COLUMNS, install, make_policy, make_semantics
from apps.core import registry
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import client_for, member_of, org_url


# ================================================================================================ unit (no DB)
def test_parse_range_defaults_and_validation():
    today = date(2026, 11, 20)
    assert usage.parse_range(None, None, today=today) == (date(2026, 10, 22), today)
    assert usage.parse_range("2026-11-01", "2026-11-03", today=today) == (date(2026, 11, 1), date(2026, 11, 3))
    for bad in (("2026-11-05", "2026-11-01"), ("yesterday", None), ("2025-01-01", "2026-11-20")):
        with pytest.raises(usage.UsageRangeError):
            usage.parse_range(*bad, today=today)
    lo, hi = usage.bounds(date(2026, 11, 1), date(2026, 11, 2))
    assert (lo, hi) == (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 3, tzinfo=UTC))


def test_ai_metric_filters_the_ai_service_rows_to_the_range(monkeypatch):
    from apps.ai import services as ai_services

    seen = {}

    def fake_summary(days):
        seen["days"] = days
        return {"range": {"from": "2026-11-10", "timezone": "America/Los_Angeles"}, "daily": [
            {"day": "2026-11-09", "model": "m", "requests": 100, "input_tokens": 1, "output_tokens": 1},
            {"day": "2026-11-18", "model": "fast", "requests": 3, "input_tokens": 30, "output_tokens": 12,
             "blocked": 1, "errors": 0},
            {"day": "2026-11-18", "model": "reasoning", "requests": 2, "input_tokens": 5, "output_tokens": 5,
             "blocked": 0, "errors": 1},
        ]}

    monkeypatch.setattr(ai_services, "usage_summary", fake_summary)
    got = usage.ai(date(2026, 11, 17), date(2026, 11, 19), today=date(2026, 11, 20))
    assert seen["days"] == 5
    assert (got["requests"], got["tokens"], got["blocked"], got["errors"]) == (5, 52, 1, 1)
    assert got["per_day"] == [{"day": "2026-11-17", "requests": 0, "tokens": 0},
                              {"day": "2026-11-18", "requests": 5, "tokens": 52},
                              {"day": "2026-11-19", "requests": 0, "tokens": 0}]


def test_a_failing_metric_is_reported_unavailable(monkeypatch):
    monkeypatch.setattr(usage, "storage", lambda s, e: 1 / 0)
    monkeypatch.setattr(usage.transaction, "atomic", lambda *a, **k: _Null())
    report = usage.usage_report(date(2026, 11, 1), date(2026, 11, 2), metric="storage")
    assert report["metrics"] == {"storage": {"available": False}} and report["range"]["days"] == 2


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_sample_sql_uses_only_policy_columns_in_dataset_order_and_quotes():
    policy = make_policy(deny=("margin", "amount"))
    table, sql = policy_sample.sample_sql(policy, {"sales": SALES_COLUMNS}, 500)
    assert table == "sales"
    assert sql == 'SELECT "region", "order_date", "customer_email" FROM "sales" LIMIT 50'
    assert policy_sample.quote_ident('we"ird') == '"we""ird"'
    with pytest.raises(LookupError):
        policy_sample.sample_sql(make_policy(deny=SALES_COLUMNS), {"sales": SALES_COLUMNS}, 5)


def test_the_bi_app_registers_the_policy_sampler():
    assert registry.policy_sampler() is policy_sample.sample_as


# ================================================================================================ DB: usage
def _audit(org, user, verdict="rejected", purpose="manual", when=None):
    from apps.bi.models import BiQueryAudit
    from apps.core.tenancy.models import uuid7

    with tenant_context(org.id):
        row = BiQueryAudit.objects.create(id=uuid7(), occurred_at=when or timezone.now(), user_id=user.pk,
                                          dataset_id=DATASET_ID, purpose=purpose, source="manual", verdict=verdict,
                                          raw_sql="SELECT 1", target="warehouse")
    return row


@pytest.mark.django_db
def test_usage_report_counts_real_rows(org, owner, owner_client, other_org):
    from apps.ai.models import AIUsageDaily
    from apps.ai.timeutil import pacific_day

    analyst = member_of(org, "analyst")
    _audit(org, owner, "ok", "chat")
    _audit(org, owner, "rejected")
    _audit(org, analyst, "ok", "dashboard")
    _audit(org, analyst, "ok", when=timezone.now() - timedelta(days=60))  # outside the default 30 days
    _audit(other_org, owner, "ok")  # another org: never counted
    with tenant_context(org.id):
        AIUsageDaily.objects.create(day=pacific_day(), model="gemini-fast", requests=4, input_tokens=100,
                                    output_tokens=20)
    r = owner_client.get(org_url(org, "usage"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["range"]["days"] == 30 and r["Cache-Control"] == "no-store"
    q = body["metrics"]["queries"]
    assert q["total"] == 3 and q["by_verdict"] == {"ok": 2, "rejected": 1}
    assert sum(d["count"] for d in q["per_day"]) == 3 and len(q["per_day"]) == 30
    assert {u["email"]: u["count"] for u in q["per_user"]} == {owner.email: 2, analyst.email: 1}
    act = body["metrics"]["active_users"]
    assert act["distinct"] == 2 and act["members_active"] == 2
    assert body["metrics"]["ai"]["requests"] == 4 and body["metrics"]["ai"]["tokens"] == 120
    for name in ("datasets", "storage", "pipeline_runs", "documents"):
        assert "available" not in body["metrics"][name] or body["metrics"][name]["available"] is not False, name
    assert body["metrics"]["storage"]["total_bytes"] >= 0


@pytest.mark.django_db
def test_usage_metric_filter_range_and_per_user_privacy(org, owner, owner_client):
    _audit(org, owner, "ok")
    one = owner_client.get(org_url(org, "usage"), {"metric": "queries", "from": date.today().isoformat()}).json()
    assert list(one["metrics"]) == ["queries"] and one["range"]["days"] >= 1
    assert owner_client.get(org_url(org, "usage"), {"metric": "nope"}).status_code == 400
    assert owner_client.get(org_url(org, "usage"), {"from": "2026-12-01", "to": "2026-11-01"}).status_code == 400
    viewer = client_for(member_of(org, "viewer"))  # org.view but not members.view
    q = viewer.get(org_url(org, "usage"), {"metric": "queries"}).json()["metrics"]["queries"]
    assert "per_user" not in q and q["total"] == 1


@pytest.mark.django_db
def test_usage_needs_membership(org, other_org, anon_client):
    outsider = client_for(member_of(other_org, "owner"))
    assert outsider.get(org_url(org, "usage")).status_code == 404
    assert anon_client.get(org_url(org, "usage")).status_code == 401


# ================================================================================================ DB: preview sample
@pytest.fixture
def sample_env(monkeypatch, org):
    fake = install(monkeypatch, org_id=org.id)
    fake.semantics[DATASET_ID] = make_semantics()
    sem = fake.semantics[DATASET_ID]
    saved_exists = dict(registry._exists)
    saved_schema = list(registry._dataset_schema_provider)
    registry.register_asset_exists("dataset", lambda asset_id: asset_id == DATASET_ID)
    registry.register_dataset_schema_provider(lambda ds: (dict(sem.tables), {t: list(c) for t, c in
                                                                            sem.columns_by_table.items()}))
    yield fake
    registry._exists.clear()
    registry._exists.update(saved_exists)
    registry._dataset_schema_provider[:] = saved_schema


@pytest.mark.django_db
def test_policy_preview_sample_runs_under_member_and_caller_policy(org, owner, owner_client, sample_env):
    from apps.bi.models import BiQueryAudit
    from apps.governance.models import AccessHistory

    analyst = member_of(org, "analyst")
    base = org_url(org, f"datasets/{DATASET_ID}/column-policies")
    assert owner_client.post(base, {"column_name": "margin", "principal_type": "user",
                                    "principal_id": str(analyst.id), "action": "deny"}, format="json").status_code == 201
    owner_client.post(base, {"column_name": "customer_email", "principal_type": "everyone", "action": "mask_hash"},
                      format="json")
    # the OWNER may not see `amount`: the preview must not reveal it even though the analyst may
    owner_client.post(base, {"column_name": "amount", "principal_type": "user", "principal_id": str(owner.id),
                             "action": "deny"}, format="json")
    r = owner_client.post(org_url(org, f"datasets/{DATASET_ID}/policy-preview"),
                          {"user_id": str(analyst.id), "sample": True, "limit": 5}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert "amount" in body["policy"]["allowed_columns"]["sales"]  # the member's own policy is reported as is
    sample = body["sample"]
    assert sample["ok"] is True and sample["row_count"] == 4 and sample["query_audit_id"]
    sql = sample_env.calls[[c[0] for c in sample_env.calls].index("validate")][1]
    assert sql == 'SELECT "region", "order_date", "customer_email" FROM "sales" LIMIT 5'
    used = sample_env.policies[-1]
    assert used.allowed_columns["sales"] == frozenset({"region", "order_date", "customer_email"})
    assert used.column_masks[("sales", "customer_email")].kind == "mask_hash"
    with tenant_context(org.id):
        audit = BiQueryAudit.objects.get(id=sample["query_audit_id"])
        assert (audit.purpose, audit.user_id, audit.verdict) == ("policy_preview", owner.pk, "ok")
        assert str(analyst.id) in audit.question
        assert AccessHistory.objects.filter(query_audit_id=audit.id, user_id=owner.pk).exists()


@pytest.mark.django_db
def test_policy_preview_sample_rejection_is_reported_and_audited(org, owner_client, sample_env):
    from apps.bi.models import BiQueryAudit

    analyst = member_of(org, "analyst")
    sql = 'SELECT "region", "amount", "order_date", "customer_email", "margin" FROM "sales" LIMIT 20'
    sample_env.rejections[sql] = ("column_not_allowed", "nope")
    r = owner_client.post(org_url(org, f"datasets/{DATASET_ID}/policy-preview"),
                          {"user_id": str(analyst.id), "sample": True}, format="json")
    assert r.status_code == 200 and r.json()["sample"]["ok"] is False
    assert r.json()["sample"]["code"] == "column_not_allowed"
    with tenant_context(org.id):
        assert BiQueryAudit.objects.get(id=r.json()["sample"]["query_audit_id"]).verdict == "rejected"


@pytest.mark.django_db
def test_policy_preview_without_sample_reads_no_data(org, owner_client, sample_env):
    analyst = member_of(org, "analyst")
    r = owner_client.post(org_url(org, f"datasets/{DATASET_ID}/policy-preview"), {"user_id": str(analyst.id)},
                          format="json")
    assert r.status_code == 200 and "sample" not in r.json()
    assert "execute" not in sample_env.names()
    assert owner_client.post(org_url(org, f"datasets/{DATASET_ID}/policy-preview"),
                             {"user_id": str(analyst.id), "sample": True, "tables": {"sales": "x.y"},
                              "columns_by_table": {"sales": ["region"]}}, format="json").status_code == 400
    assert owner_client.post(org_url(org, f"datasets/{uuid4()}/policy-preview"),
                             {"user_id": str(analyst.id), "sample": True}, format="json").status_code == 404
