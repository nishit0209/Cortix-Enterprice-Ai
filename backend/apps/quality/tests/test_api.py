"""Quality rules API, re-evaluation job and blocking rules during ingestion (real DB + warehouse)."""

import pytest

from apps.core.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.datasets.tests.helpers import SAMPLE_CUSTOMERS, add_member, csv_bytes, ingest, upload_file
from apps.quality.models import QualityResult, QualityRule

pytestmark = pytest.mark.django_db


def url(org, path=""):
    return f"/api/v1/orgs/{org.slug}/{path}"


@pytest.fixture
def ready(api_client, two_orgs, fake_storage, django_capture_on_commit_callbacks):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    _, ds = ingest(api_client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    assert ds["status"] == "ready", ds["last_error"]
    return api_client, org_a, org_b, bob, ds


def test_suggested_rules_are_listed_with_last_results(ready):
    client, org_a, _, _, ds = ready
    rules = client.get(url(org_a, f"datasets/{ds['id']}/quality-rules")).json()
    kinds = {(r["rule_type"], r["column"]) for r in rules}
    assert ("unique", "customer_id") in kinds and ("not_null", "email") in kinds
    assert ("allowed_values", "city") in kinds
    assert all(r["origin"] == "suggested_profile" and r["last_result"]["passed"] for r in rules)


def test_create_rule_validate_patch_delete(ready):
    client, org_a, _, _, ds = ready
    base = url(org_a, f"datasets/{ds['id']}/quality-rules")
    bad = client.post(base, {"rule_type": "range", "column": "customer_id", "params": {}}, format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "params"
    unknown = client.post(base, {"rule_type": "not_null", "column": "nope"}, format="json")
    assert unknown.status_code == 400
    created = client.post(
        base,
        {"rule_type": "range", "column": "lifetime_value_inr", "params": {"max": 10000}, "severity": "error"},
        format="json",
    )
    assert created.status_code == 201
    rule = created.json()
    assert rule["origin"] == "manual" and rule["params"] == {"min": None, "max": 10000, "inclusive": True}

    detail = url(org_a, f"datasets/{ds['id']}/quality-rules/{rule['id']}")
    patched = client.patch(detail, {"severity": "warn", "is_blocking": True}, format="json")
    assert patched.status_code == 200 and patched.json()["severity"] == "warn" and patched.json()["is_blocking"]
    assert client.patch(detail, {"params": {"min": 5, "max": 1}}, format="json").status_code == 400
    assert client.get(detail).status_code == 200
    assert client.delete(detail).status_code == 204
    assert client.get(detail).status_code == 404
    with tenant_context(org_a.id):
        actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"quality_rule.create", "quality_rule.update", "quality_rule.delete"} <= actions


def test_expression_rule_and_reevaluation_job(ready, django_capture_on_commit_callbacks):
    client, org_a, _, _, ds = ready
    base = url(org_a, f"datasets/{ds['id']}/quality-rules")
    rule = client.post(base, {"rule_type": "expression", "params": {"expression": "customer_id < 1020"},
                              "severity": "error"}, format="json").json()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(org_a, f"datasets/{ds['id']}/quality/evaluate"))
    assert resp.status_code == 202
    job = client.get(url(org_a, f"jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    listed = {r["id"]: r for r in client.get(base).json()}
    last = listed[rule["id"]]["last_result"]
    assert last["passed"] is False and last["failed_count"] == 20 and last["evaluated_count"] == 40
    quality = client.get(url(org_a, f"datasets/{ds['id']}/quality")).json()
    assert quality["score"] < ds["quality_score"]
    failing = next(r for r in quality["rules"] if r["id"] == rule["id"])
    assert failing["failed_count"] == 20 and len(failing["sample_failures"]) == 20
    detail = client.get(url(org_a, f"datasets/{ds['id']}")).json()
    assert detail["quality_score"] == quality["score"]


def test_blocking_rule_keeps_previous_version_current(ready, django_capture_on_commit_callbacks):
    client, org_a, _, _, ds = ready
    client.post(url(org_a, f"datasets/{ds['id']}/quality-rules"),
                {"rule_type": "row_count", "params": {"max": 10}, "severity": "error", "is_blocking": True},
                format="json")
    up = upload_file(client, org_a, "v2.csv", csv_bytes(SAMPLE_CUSTOMERS)).json()
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(org_a, f"datasets/{ds['id']}/versions"), {"upload_id": up["id"]}, format="json")
    job = client.get(url(org_a, f"jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded" and job["result"]["current"] is False and job["result"]["blocking_failures"]
    detail = client.get(url(org_a, f"datasets/{ds['id']}")).json()
    assert detail["current_version"]["version"] == 1 and detail["status"] == "ready"
    assert detail["last_error"]["code"] == "quality_blocking_failure"
    v2 = client.get(url(org_a, f"datasets/{ds['id']}/quality?version=2")).json()
    assert v2["status"] == "ready" and v2["summary"]["blocking_failures"]


def test_quality_permissions_and_isolation(ready):
    client, org_a, org_b, bob, ds = ready
    viewer = add_member(org_a, "qviewer@example.test", "viewer")
    client.force_authenticate(viewer)
    base = url(org_a, f"datasets/{ds['id']}/quality-rules")
    assert client.get(base).status_code == 200
    assert client.get(url(org_a, f"datasets/{ds['id']}/quality")).status_code == 200
    assert client.post(base, {"rule_type": "not_null", "column": "email"}, format="json").status_code == 403
    assert client.post(url(org_a, f"datasets/{ds['id']}/quality/evaluate")).status_code == 403
    client.force_authenticate(bob)
    assert client.get(url(org_b, f"datasets/{ds['id']}/quality-rules")).status_code == 404
    assert client.post(url(org_b, f"datasets/{ds['id']}/quality/evaluate")).status_code == 404
    with tenant_context(org_b.id):
        assert QualityRule.objects.count() == 0 and QualityResult.objects.count() == 0
