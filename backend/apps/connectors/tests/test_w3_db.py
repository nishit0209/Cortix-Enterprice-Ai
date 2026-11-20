"""W3 data sources (DB-backed: real Postgres under RLS; connector faked, storage faked, ingest real).

The catalog browser's request shapes (`table: "schema.table"`, `row_estimate`, connection `checks`) and the import
notifications to the user who started the import.
"""

import pytest

from apps.connectors import services
from apps.connectors.models import SourceImport

from .fakes import ORDERS_COLUMNS, ORDERS_ROWS, FakeConnector
from .test_api import create, url

pytestmark = pytest.mark.django_db


@pytest.fixture
def connector(monkeypatch):
    fake = FakeConnector(tables={"orders": (ORDERS_COLUMNS, list(ORDERS_ROWS))})
    monkeypatch.setattr(services, "build_connector", lambda source: fake)
    return fake


@pytest.fixture
def env(two_orgs, api_client_for, quiet_redis, fake_storage, connector):
    a, b = two_orgs
    return a, b, api_client_for(a.owner)


def test_connection_test_stores_ui_checks(env, django_capture_on_commit_callbacks, monkeypatch):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    checks = client.get(url(a, f"data-sources/{sid}")).json()["last_test_result"]["checks"]
    assert [c["key"] for c in checks] == ["dns", "ssrf", "tls", "login", "readonly"] and all(c["ok"] for c in checks)
    monkeypatch.setattr(services, "build_connector", lambda source: FakeConnector(readonly=False))
    with django_capture_on_commit_callbacks(execute=True):
        job = client.post(url(a, f"data-sources/{sid}/test")).json()
    failed = client.get(url(a, f"jobs/{job['job_id']}")).json()["error"]["detail"]["checks"]
    assert failed[-1]["key"] == "readonly" and failed[-1]["ok"] is False
    assert "write:sales.orders" in failed[-1]["message"]


def test_catalog_lists_the_row_estimate(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        job = client.post(url(a, f"data-sources/{sid}/introspect")).json()
    result = client.get(url(a, f"jobs/{job['job_id']}")).json()["result"]
    assert result["schemas"][0]["tables"][0]["row_estimate"] == 30


def test_import_of_a_qualified_table_name_notifies_the_importer(env, django_capture_on_commit_callbacks,
                                                                captured_notifications):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url(a, f"data-sources/{sid}/introspect"))  # the stored catalog lists sales.orders
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, f"data-sources/{sid}/import"), {"table": "sales.orders", "name": "Orders"},
                           format="json")
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["import"]["schema"] == "sales" and body["import"]["table"] == "orders"
    with a.tenant():
        imp = SourceImport.objects.get(id=body["import"]["id"])
    assert imp.status == "succeeded" and imp.rows == 30
    assert [n["kind"] for n in captured_notifications] == ["import.finished", "dataset.ingest_finished"]
    finished, ready = captured_notifications
    assert finished["user_ids"] == ready["user_ids"] == [str(a.owner.pk)]
    assert finished["title"] == "Import of sales.orders finished"
    assert finished["body"].startswith("30 rows read from Shop replica into 'Orders'.")
    assert finished["link"] == ready["link"] == f"/o/{a.slug}/data/datasets/{imp.dataset_id}"


def test_failed_import_notifies_the_importer(env, django_capture_on_commit_callbacks, captured_notifications):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, f"data-sources/{sid}/import"), {"table": "nope", "name": "x"}, format="json")
    assert resp.status_code == 202
    (note,) = captured_notifications
    assert note["kind"] == "import.failed" and note["user_ids"] == [str(a.owner.pk)]
    assert note["title"] == "Import of sales.nope failed" and "(connector_table_not_found)" in note["body"]
    assert note["link"] == f"/o/{a.slug}/data/sources"


def test_a_dotted_name_outside_the_catalog_stays_one_table(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url(a, f"data-sources/{sid}/introspect"))
    resp = client.post(url(a, f"data-sources/{sid}/import"), {"table": "fy.2024", "name": "Dotted"}, format="json")
    assert resp.status_code == 202, resp.content
    assert resp.json()["import"]["schema"] == "sales" and resp.json()["import"]["table"] == "fy.2024"
    too_long = client.post(url(a, f"data-sources/{sid}/import"), {"table": "t" * 64, "name": "Long"}, format="json")
    assert too_long.status_code == 400
