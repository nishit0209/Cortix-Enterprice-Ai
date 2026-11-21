"""Data-source API + jobs (DB-backed: real Postgres under RLS; connector faked, storage faked, ingest real).

Outbound connections are replaced by `FakeConnector` through `services.build_connector`, so these tests cover the
API, permissions, encryption at rest, job bodies, import -> upload -> ingest hand-off and lineage.
"""

import json
import logging

import pytest

from apps.connectors import services
from apps.connectors.models import Credential, DataSource, SourceImport
from apps.core.crypto import decrypt_secret
from apps.core.models import AuditEvent, Job

from .fakes import ORDERS_COLUMNS, ORDERS_ROWS, FakeConnector

pytestmark = pytest.mark.django_db

CANARY = "Synthetic-Canary-Pw-7d1e"  # synthetic secret that must never leave the credential table
CONFIG = {"host": "db.example.com", "port": 5432, "dbname": "shop", "sslmode": "require", "schema": "sales"}


def url(world, path=""):
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def connector(monkeypatch):
    fake = FakeConnector(tables={"orders": (ORDERS_COLUMNS, list(ORDERS_ROWS))})
    monkeypatch.setattr(services, "build_connector", lambda source: fake)
    return fake


@pytest.fixture
def env(two_orgs, api_client_for, quiet_redis, fake_storage, connector):
    a, b = two_orgs
    return a, b, api_client_for(a.owner)


def create(client, world, capture, **overrides):
    body = {"name": "Shop replica", "type": "postgresql", "config": CONFIG, "username": "nexora_ro",
            "password": CANARY, **overrides}
    with capture(execute=True):
        return client.post(url(world, "data-sources"), body, format="json")


def test_create_tests_and_activates_source(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    resp = create(client, a, django_capture_on_commit_callbacks)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert "password" not in body and body["has_password"] is True and CANARY not in resp.content.decode()
    assert body["test_job"]["events_url"].endswith("/events")
    detail = client.get(url(a, f"data-sources/{body['id']}")).json()
    assert detail["status"] == "active" and detail["verified_readonly"] is True
    assert detail["last_test_result"]["readonly"] == {"ok": True, "problems": []}
    assert detail["resolved_ip"] == "52.10.20.30"
    with a.tenant():
        job = Job.objects.get(id=body["test_job"]["job_id"])
        assert job.status == "succeeded" and job.type == "connector_test"
        assert AuditEvent.objects.filter(action="data_source.create", target_id=body["id"]).exists()
        assert AuditEvent.objects.filter(action="data_source.test", target_id=body["id"]).exists()


def test_password_is_encrypted_at_rest_and_never_exposed(env, django_capture_on_commit_callbacks, caplog):
    a, _b, client = env
    with caplog.at_level(logging.DEBUG):
        resp = create(client, a, django_capture_on_commit_callbacks)
        sid = resp.json()["id"]
        listed = client.get(url(a, "data-sources"))
        detail = client.get(url(a, f"data-sources/{sid}"))
    for r in (resp, listed, detail):
        assert CANARY not in r.content.decode()
    assert CANARY not in caplog.text
    with a.tenant():
        source = DataSource.objects.get(id=sid)
        cred = Credential.objects.get(id=source.credential_id)
        assert CANARY not in json.dumps(cred.blob) and CANARY not in cred.aad
        assert cred.aad == f"org:{a.org.id}:credential:{cred.id}"
        assert decrypt_secret(cred.blob, aad=cred.aad) == CANARY
        for ev in AuditEvent.objects.all():
            assert CANARY not in json.dumps(ev.metadata) and CANARY not in json.dumps(ev.changes or {})
        for job in Job.objects.all():
            assert CANARY not in json.dumps(job.result or {}) and CANARY not in json.dumps(job.error or {})
        assert CANARY not in repr(cred) and CANARY not in str(source.config)


def test_ciphertext_is_bound_to_its_credential_row(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with a.tenant():
        cred = DataSource.objects.get(id=sid).credential
        with pytest.raises(Exception):
            decrypt_secret(cred.blob, aad=f"org:{a.org.id}:credential:00000000-0000-0000-0000-000000000000")


def test_write_role_is_refused(env, django_capture_on_commit_callbacks, monkeypatch):
    a, _b, client = env
    monkeypatch.setattr(services, "build_connector", lambda source: FakeConnector(readonly=False))
    resp = create(client, a, django_capture_on_commit_callbacks)
    assert resp.status_code == 201
    detail = client.get(url(a, f"data-sources/{resp.json()['id']}")).json()
    assert detail["status"] == "error" and detail["verified_readonly"] is False
    assert detail["last_error"]["code"] == "connector_role_not_readonly"
    with a.tenant():
        job = Job.objects.get(id=resp.json()["test_job"]["job_id"])
        assert job.status == "failed" and job.error["code"] == "connector_role_not_readonly"
    imp = client.post(url(a, f"data-sources/{resp.json()['id']}/import"), {"table": "orders", "name": "x"},
                      format="json")
    assert imp.status_code == 409 and imp.json()["code"] == "data_source_not_verified"


@pytest.mark.parametrize("config,status,code", [
    ({**CONFIG, "host": "127.0.0.1"}, 422, "connector_host_forbidden"),
    ({**CONFIG, "host": "169.254.169.254"}, 422, "connector_host_forbidden"),
    ({**CONFIG, "host": "localhost"}, 422, "connector_host_forbidden"),
    ({**CONFIG, "options": "-c role=postgres"}, 400, "invalid"),
    ({**CONFIG, "sslmode": "disable"}, 400, "invalid"),
    ({**CONFIG, "host": "http://db.example.com/x"}, 400, "connector_host_invalid"),
])
def test_create_rejects_unsafe_config(env, django_capture_on_commit_callbacks, settings, config, status, code):
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    a, _b, client = env
    resp = create(client, a, django_capture_on_commit_callbacks, config=config)
    assert resp.status_code == status, resp.content
    assert resp.json()["code"] == code
    with a.tenant():
        assert not DataSource.objects.exists() and not Credential.objects.exists()


def test_duplicate_name_conflicts(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    create(client, a, django_capture_on_commit_callbacks)
    resp = create(client, a, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and resp.json()["code"] == "data_source_name_taken"


def test_patch_rotates_password_and_retests(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    renamed = client.patch(url(a, f"data-sources/{sid}"), {"name": "Renamed"}, format="json")
    assert renamed.status_code == 200 and "test_job" not in renamed.json()
    with django_capture_on_commit_callbacks(execute=True):
        rotated = client.patch(url(a, f"data-sources/{sid}"), {"password": "Synthetic-New-Pw-1"}, format="json")
    assert rotated.status_code == 200 and "test_job" in rotated.json()
    with a.tenant():
        source = DataSource.objects.get(id=sid)
        assert source.status == "active" and source.credential.rotated_at is not None
        assert decrypt_secret(source.credential.blob, aad=source.credential.aad) == "Synthetic-New-Pw-1"
        ev = AuditEvent.objects.filter(action="data_source.update", target_id=sid).order_by("-occurred_at").first()
        assert "Synthetic-New-Pw-1" not in json.dumps(ev.changes)


def test_disable_and_delete(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    assert client.patch(url(a, f"data-sources/{sid}"), {"status": "disabled"}, format="json").json()["status"] == \
        "disabled"
    assert client.post(url(a, f"data-sources/{sid}/test")).status_code == 409
    with a.tenant():
        cred_id = DataSource.objects.get(id=sid).credential_id
    assert client.delete(url(a, f"data-sources/{sid}")).status_code == 204
    assert client.get(url(a, f"data-sources/{sid}")).status_code == 404
    with a.tenant():
        assert not Credential.objects.filter(id=cred_id).exists()
        assert AuditEvent.objects.filter(action="data_source.delete", target_id=sid).exists()


def test_delete_refused_while_a_pipeline_uses_the_source(env, django_capture_on_commit_callbacks):
    from apps.pipelines.models import Pipeline, PipelineVersion

    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with a.tenant():
        p = Pipeline.objects.create(name="Uses source", status="active")
        v = PipelineVersion.objects.create(pipeline=p, version=1, dag={"nodes": [
            {"id": "s", "type": "source.connector", "config": {"data_source_id": sid, "table": "orders"}}],
            "edges": []})
        p.current_version = v
        p.save()
    resp = client.delete(url(a, f"data-sources/{sid}"))
    assert resp.status_code == 409 and resp.json()["code"] == "data_source_in_use"
    assert resp.json()["used_by"] == ["Uses source"]


def test_introspect_job_stores_catalog(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    empty = client.get(url(a, f"data-sources/{sid}/introspect")).json()
    assert empty["catalog"]["schemas"] == []
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, f"data-sources/{sid}/introspect"))
    assert resp.status_code == 202
    catalog = client.get(url(a, f"data-sources/{sid}/introspect")).json()["catalog"]
    (schema,) = catalog["schemas"]
    assert schema["name"] == "sales" and schema["tables"][0]["name"] == "orders"
    assert [c["name"] for c in schema["tables"][0]["columns"]] == ["id", "city", "total"]
    assert schema["tables"][0]["estimated_rows"] == 30


def test_import_creates_dataset_through_the_standard_ingest(env, django_capture_on_commit_callbacks, connector):
    from apps.lineage import services as lineage

    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, f"data-sources/{sid}/import"), {"table": "orders", "name": "Orders"},
                           format="json")
    assert resp.status_code == 202, resp.content
    imp_id = resp.json()["import"]["id"]
    with a.tenant():
        imp = SourceImport.objects.get(id=imp_id)
        assert imp.status == "succeeded" and imp.rows == 30 and imp.dataset_id is not None
        job = Job.objects.get(id=resp.json()["job_id"])
        assert job.status == "succeeded" and job.result["dataset_id"] == str(imp.dataset_id)
        graph = lineage.graph("dataset", imp.dataset_id, depth=2)
        assert any(n["id"] == sid and n["type"] == "data_source" and n["label"] == "Shop replica"
                   for n in graph["nodes"])
    ds = client.get(url(a, f"datasets/{imp.dataset_id}")).json()
    assert ds["status"] == "ready" and ds["row_count"] == 30
    cols = [c["name"] for c in client.get(url(a, f"datasets/{imp.dataset_id}/columns")).json()]
    assert cols == ["id", "city", "total"]
    listed = client.get(url(a, f"data-sources/{sid}/imports")).json()["items"]
    assert listed[0]["id"] == imp_id

    # re-import -> a new version of the same dataset
    with django_capture_on_commit_callbacks(execute=True):
        again = client.post(url(a, f"data-sources/{sid}/import"), {"table": "orders", "name": "Orders"},
                            format="json")
    assert again.json()["import"]["dataset_id"] == str(imp.dataset_id)
    versions = client.get(url(a, f"datasets/{imp.dataset_id}/versions")).json()
    assert len(versions) == 2


def test_incremental_import_appends_new_rows(env, django_capture_on_commit_callbacks, connector):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    body = {"table": "orders", "name": "Orders inc", "mode": "incremental", "cursor_column": "id"}
    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(url(a, f"data-sources/{sid}/import"), body, format="json").json()
    with a.tenant():
        imp = SourceImport.objects.get(id=first["import"]["id"])
        assert imp.cursor_value == "30"
    connector.tables["orders"][1].extend([(31, "Pune", "310.50"), (32, "Delhi", "320.50")])
    with django_capture_on_commit_callbacks(execute=True):
        second = client.post(url(a, f"data-sources/{sid}/import"), body, format="json").json()
    assert connector.reads[-1]["after"] == "30"
    with a.tenant():
        imp2 = SourceImport.objects.get(id=second["import"]["id"])
        assert imp2.rows == 2 and imp2.cursor_value == "32" and imp2.dataset_id == imp.dataset_id
    ds = client.get(url(a, f"datasets/{imp.dataset_id}")).json()
    assert ds["row_count"] == 32


def test_import_validation_and_attestation(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    bad_schema = client.post(url(a, f"data-sources/{sid}/import"), {"table": "orders", "name": "x", "schema": "hr"},
                             format="json")
    assert bad_schema.status_code == 400
    no_cursor = client.post(url(a, f"data-sources/{sid}/import"),
                            {"table": "orders", "name": "x", "mode": "incremental"}, format="json")
    assert no_cursor.status_code == 400
    unattested = client.post(url(a, f"data-sources/{sid}/import"),
                             {"table": "orders", "name": "x", "classification": "synthetic"}, format="json")
    assert unattested.status_code == 422 and unattested.json()["code"] == "attestation_required"


def test_import_of_missing_table_fails_the_job(env, django_capture_on_commit_callbacks):
    a, _b, client = env
    sid = create(client, a, django_capture_on_commit_callbacks).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, f"data-sources/{sid}/import"), {"table": "nope", "name": "x"}, format="json")
    with a.tenant():
        job = Job.objects.get(id=resp.json()["job_id"])
        assert job.status == "failed" and job.error["code"] == "connector_table_not_found"
        assert SourceImport.objects.get(id=resp.json()["import"]["id"]).status == "failed"


def test_permissions(two_orgs, api_client_for, quiet_redis, fake_storage, connector,
                     django_capture_on_commit_callbacks):
    a, _b = two_orgs
    owner = api_client_for(a.owner)
    sid = create(owner, a, django_capture_on_commit_callbacks).json()["id"]
    analyst = api_client_for(a.analyst)
    assert analyst.get(url(a, "data-sources")).status_code == 200
    assert analyst.get(url(a, f"data-sources/{sid}/introspect")).status_code == 200
    assert analyst.post(url(a, f"data-sources/{sid}/introspect")).status_code == 403
    assert analyst.post(url(a, f"data-sources/{sid}/test")).status_code == 403
    assert analyst.post(url(a, "data-sources"), {"name": "x"}, format="json").status_code == 403
    assert analyst.delete(url(a, f"data-sources/{sid}")).status_code == 403
    viewer = api_client_for(a.viewer)
    assert viewer.get(url(a, "data-sources")).status_code == 403
    engineer = api_client_for(a.user("data_engineer"))
    with django_capture_on_commit_callbacks(execute=True):
        assert engineer.post(url(a, f"data-sources/{sid}/import"), {"table": "orders", "name": "E"},
                             format="json").status_code == 202


def test_cross_org_access_is_404(two_orgs, api_client_for, quiet_redis, fake_storage, connector,
                                 django_capture_on_commit_callbacks):
    a, b = two_orgs
    sid = create(api_client_for(a.owner), a, django_capture_on_commit_callbacks).json()["id"]
    other = api_client_for(b.owner)
    for method, path in (("get", f"data-sources/{sid}"), ("patch", f"data-sources/{sid}"),
                         ("delete", f"data-sources/{sid}"), ("post", f"data-sources/{sid}/test"),
                         ("post", f"data-sources/{sid}/introspect"), ("get", f"data-sources/{sid}/introspect"),
                         ("post", f"data-sources/{sid}/import"), ("get", f"data-sources/{sid}/imports")):
        resp = getattr(other, method)(url(b, path), {"table": "orders", "name": "x"} if method == "post" else None,
                                      format="json")
        assert resp.status_code == 404, (method, path, resp.status_code)
        resp_a = getattr(other, method)(url(a, path), format="json")
        assert resp_a.status_code == 404
    assert other.get(url(b, "data-sources")).json()["items"] == []
    with b.tenant():
        assert not DataSource.objects.filter(id=sid).exists()
