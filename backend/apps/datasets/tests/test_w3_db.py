"""W3 datasets (DB-backed: real Postgres under RLS + nexora_wh; storage faked).

Ingestion notifications go to the uploader (ready / failed / blocked by a quality gate), the ingest-finished signal
is delivered once per terminal job, data-stats carries the connector source counts, and - once the Backend
notifications service is merged - real `core.Notification` rows are written.
"""

from __future__ import annotations

import pytest

from apps.core.tenancy import tenant_context
from apps.datasets.tests.helpers import SAMPLE_CUSTOMERS, add_member, csv_bytes, ingest, upload_file

pytestmark = pytest.mark.django_db


def url(org, path=""):
    return f"/api/v1/orgs/{org.slug}/{path}"


@pytest.fixture
def ctx(api_client, two_orgs, fake_storage):
    alice, org_a, bob, org_b = two_orgs
    api_client.force_authenticate(alice)
    return api_client, alice, org_a, bob, org_b


@pytest.fixture
def finished():
    """Deliveries of `dataset_ingest_finished`."""
    from apps.datasets.signals import dataset_ingest_finished

    seen: list[dict] = []

    def receiver(sender, **kwargs):
        seen.append(kwargs)

    dataset_ingest_finished.connect(receiver, dispatch_uid="tests.datasets.w3.finished")
    yield seen
    dataset_ingest_finished.disconnect(dispatch_uid="tests.datasets.w3.finished")


def test_a_ready_dataset_notifies_its_uploader(ctx, django_capture_on_commit_callbacks, captured_notifications,
                                               finished):
    client, alice, org_a, *_ = ctx
    resp, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    assert ds["status"] == "ready", ds["last_error"]
    (note,) = captured_notifications
    assert note["kind"] == "dataset.ingest_finished" and note["user_ids"] == [str(alice.pk)]
    assert note["title"] == "Dataset 'Customers' is ready"
    assert note["body"].startswith("Version 1: 40 rows, quality score ")
    assert note["link"] == f"/o/{org_a.slug}/data/datasets/{ds['id']}"
    (signal,) = finished
    assert signal["ok"] is True and signal["code"] is None
    assert str(signal["dataset_id"]) == ds["id"] and str(signal["job_id"]) == resp.json()["job_id"]
    assert signal["org_id"] == org_a.id


def test_the_uploader_not_the_owner_is_notified(ctx, django_capture_on_commit_callbacks, captured_notifications):
    client, alice, org_a, *_ = ctx
    engineer = add_member(org_a, "engineer-w3@example.test", "data_engineer")
    client.force_authenticate(engineer)
    ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    assert [n["user_ids"] for n in captured_notifications] == [[str(engineer.pk)]]


def test_a_failed_ingestion_notifies_its_uploader(ctx, django_capture_on_commit_callbacks, captured_notifications,
                                                  finished):
    client, alice, org_a, *_ = ctx
    resp, ds = ingest(client, org_a, "bad.csv", b"a,b\n1,2\n3,4,5,6\n", django_capture_on_commit_callbacks, name="Bad")
    assert ds["status"] == "failed"
    (note,) = captured_notifications
    assert note["kind"] == "dataset.ingest_failed" and note["user_ids"] == [str(alice.pk)]
    assert note["title"] == "Ingesting 'Bad' failed" and "(malformed_file)" in note["body"]
    (signal,) = finished
    assert signal["ok"] is False and signal["code"] == "malformed_file"
    assert str(signal["job_id"]) == resp.json()["job_id"]


def test_a_version_blocked_by_a_quality_gate_is_reported(ctx, django_capture_on_commit_callbacks,
                                                         captured_notifications):
    client, alice, org_a, *_ = ctx
    _resp, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    rule = client.post(url(org_a, f"datasets/{ds['id']}/quality-rules"), {
        "rule_type": "row_count", "params": {"min": 100000}, "severity": "error", "is_blocking": True}, format="json")
    assert rule.status_code == 201, rule.content
    up = upload_file(client, org_a, "v2.csv", csv_bytes(SAMPLE_CUSTOMERS)).json()
    with django_capture_on_commit_callbacks(execute=True):
        assert client.post(url(org_a, f"datasets/{ds['id']}/versions"), {"upload_id": up["id"]},
                           format="json").status_code == 202
    kinds = [n["kind"] for n in captured_notifications]
    assert kinds == ["dataset.ingest_finished", "dataset.ingest_failed"]
    blocked = captured_notifications[-1]
    assert blocked["title"] == "Dataset 'Customers' was not published"
    assert "Version 2 failed 1 blocking quality rule(s)" in blocked["body"]
    assert client.get(url(org_a, f"datasets/{ds['id']}")).json()["current_version"]["version"] == 1


def test_ingest_job_result_carries_rule_counts(ctx, django_capture_on_commit_callbacks):
    client, _alice, org_a, *_ = ctx
    resp, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    result = client.get(url(org_a, f"jobs/{resp.json()['job_id']}")).json()["result"]
    quality = client.get(url(org_a, f"datasets/{ds['id']}/quality")).json()
    assert result["rules_passed"] == sum(1 for r in quality["rules"] if r["passed"])
    assert result["rules_failed"] == sum(1 for r in quality["rules"] if not r["passed"])


def test_data_stats_counts_sources_and_dataset_states(ctx, django_capture_on_commit_callbacks):
    from apps.connectors.models import DataSource

    client, _alice, org_a, _bob, org_b = ctx
    ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    ingest(client, org_a, "bad.csv", b"a,b\n1,2\n3,4,5,6\n", django_capture_on_commit_callbacks, name="Bad")
    with tenant_context(org_a.id):
        DataSource.objects.create(name="Replica", type="postgresql", status="active", config={"host": "db.example"})
        DataSource.objects.create(name="Old replica", type="postgresql", status="error", config={"host": "db.example"})
    stats = client.get(url(org_a, "data-stats")).json()
    assert stats["datasets"] == 2 and stats["datasets_by_status"] == {"ready": 1, "processing": 0, "failed": 1}
    assert stats["rows_total"] == 40 and stats["sources"] == 2 and stats["sources_active"] == 1
    assert stats["pipelines_total"] == 0 and stats["runs_active"] == 0
    with tenant_context(org_b.id):
        assert DataSource.objects.count() == 0


def test_real_notifications_service_stores_rows(ctx, django_capture_on_commit_callbacks):
    """Runs once the Backend W3 `apps.core.notifications` + `core.Notification` are merged (skipped before)."""
    pytest.importorskip("apps.core.notifications")
    from django.apps import apps as django_apps

    try:
        notification_model = django_apps.get_model("core", "Notification")
    except LookupError:
        pytest.skip("core.Notification is not installed yet")
    client, alice, org_a, *_ = ctx
    _resp, ds = ingest(client, org_a, "customers.csv", csv_bytes(SAMPLE_CUSTOMERS), django_capture_on_commit_callbacks)
    with tenant_context(org_a.id):
        rows = list(notification_model.objects.filter(kind="dataset.ingest_finished").values("user_id", "title", "link"))
    assert len(rows) == 1 and str(rows[0]["user_id"]) == str(alice.pk)
    assert rows[0]["link"] == f"/o/{org_a.slug}/data/datasets/{ds['id']}"
