"""Customer 360 API (DB-backed: real Postgres under RLS + nexora_wh; storage faked; tasks run eagerly)."""

import pytest

from apps.core.models import AuditEvent
from apps.customers.models import CustomerEvent, CustomerProfile, CustomerProfileLink, IdentityMatch
from apps.datasets.tests.helpers import csv_bytes, ingest

pytestmark = pytest.mark.django_db

CRM = [
    ["crm_id", "first_name", "last_name", "email", "phone", "city", "date_of_birth"],
    ["CRM-1", "Pooja", "Kapoor", "pooja.kapoor@example.org", "9230026611", "Hyderabad", "1970-12-09"],
    ["CRM-2", "Rahul", "Mehta", "rahul.mehta@example.org", "9000000002", "Pune", "1985-01-02"],
    ["CRM-3", "Asha", "Rao", "asha.rao@example.org", "9000000003", "Chennai", "1990-03-04"],
    ["CRM-4", "Vikram", "Shah", "vikram.shah@example.org", "9000000004", "Mumbai", "1979-05-06"],
]
WEB = [
    ["web_id", "full_name", "email", "mobile", "city", "signup_date"],
    ["WEB-1", "Pooja Kapoor", "POOJA.KAPOOR@example.org", "+91-9230026611", "hyderabad", "2024-01-05"],
    ["WEB-2", "Rahul Mehta", "rahul.m@another.net", "+91 90000 00002", "Pune", "2024-02-01"],
    ["WEB-3", "Neha Iyer", "neha.iyer@example.org", "9111111111", "Delhi", "2024-03-01"],
]
ORDERS = [
    ["order_id", "customer_id", "order_date", "amount"],
    ["O-1", "CRM-1", "2026-10-01", "1200.50"],
    ["O-2", "CRM-1", "2026-10-20", "800.00"],
    ["O-3", "CRM-2", "2026-05-01", "150.00"],
    ["O-4", "CRM-3", "2025-12-01", "90.00"],
    ["O-5", "CRM-404", "2026-01-01", "10.00"],
]
MAP_CRM = {"external_id": "crm_id", "first_name": "first_name", "last_name": "last_name", "email": "email",
           "phone": "phone", "address": "city", "dob": "date_of_birth"}
MAP_WEB = {"external_id": "web_id", "name": "full_name", "email": "email", "phone": "mobile", "address": "city",
           "created_at": "signup_date"}


def url(world, path=""):
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def env(two_orgs, api_client_for, fake_redis, fake_storage, django_capture_on_commit_callbacks):
    a, b = two_orgs
    client = api_client_for(a.owner)
    cap = django_capture_on_commit_callbacks
    _r, crm = ingest(client, a, "crm.csv", csv_bytes(CRM), cap, name="CRM customers")
    _r, web = ingest(client, a, "web.csv", csv_bytes(WEB), cap, name="Web signups")
    _r, orders = ingest(client, a, "orders.csv", csv_bytes(ORDERS), cap, name="Orders")
    assert crm["status"] == web["status"] == orders["status"] == "ready"
    return a, b, client, crm, web, orders


def add_sources(client, a, crm, web, orders):
    rc = client.post(url(a, "customers/sources"), {"dataset_id": crm["id"], "mapping": MAP_CRM, "priority": 10},
                     format="json")
    assert rc.status_code == 201, rc.content
    rw = client.post(url(a, "customers/sources"), {"dataset_id": web["id"], "mapping": MAP_WEB, "priority": 20},
                     format="json")
    assert rw.status_code == 201, rw.content
    ro = client.post(url(a, "customers/sources"), {
        "dataset_id": orders["id"], "kind": "event", "link_source_id": rc.json()["id"],
        "mapping": {"customer_key": "customer_id", "occurred_at": "order_date", "amount": "amount",
                    "event_id": "order_id", "event_type": "order"}}, format="json")
    assert ro.status_code == 201, ro.content
    return rc.json(), rw.json(), ro.json()


def resolve(client, a, capture):
    with capture(execute=True):
        resp = client.post(url(a, "customers/resolve"))
    assert resp.status_code == 202, resp.content
    run = client.get(url(a, f"customers/resolution-runs/{resp.json()['run_id']}")).json()
    assert run["status"] == "succeeded", run
    return run


def profile_of(a, source_key):
    with a.tenant():
        return CustomerProfileLink.objects.get(record__source_key=source_key).profile_id


def test_sources_resolution_profiles_and_timeline(env, django_capture_on_commit_callbacks, captured_notifications):
    a, _b, client, crm, web, orders = env
    assert client.post(url(a, "customers/resolve")).json()["code"] == "customer_sources_required"
    add_sources(client, a, crm, web, orders)
    bad = client.post(url(a, "customers/sources"), {"dataset_id": crm["id"], "mapping": {"email": "nope"},
                                                    "name": "Other"}, format="json")
    assert bad.status_code == 400
    run = resolve(client, a, django_capture_on_commit_callbacks)
    assert run["stats"]["records"] == 7
    assert profile_of(a, "CRM-1") == profile_of(a, "WEB-1")  # exact email + phone -> auto merged
    assert profile_of(a, "CRM-3") != profile_of(a, "WEB-3")
    with a.tenant():
        assert CustomerProfile.objects.count() <= 6
        assert CustomerEvent.objects.filter(profile__isnull=True).count() == 1  # CRM-404 has no customer
    pooja = profile_of(a, "CRM-1")
    detail = client.get(url(a, f"customers/{pooja}")).json()
    assert detail["email"] == "pooja.kapoor@example.org" and detail["record_count"] == 2
    assert detail["order_count"] == 2 and float(detail["monetary"]) == pytest.approx(2000.5)
    timeline = client.get(url(a, f"customers/{pooja}/timeline")).json()["items"]
    assert [e["event_type"] for e in timeline] == ["order", "order"]
    assert timeline[0]["occurred_at"] > timeline[1]["occurred_at"]
    found = client.get(url(a, "customers?search=kapoor")).json()["items"]
    assert [p["id"] for p in found] == [str(pooja)]
    assert captured_notifications[-1]["kind"] == "customers.resolution_finished"
    # a re-run keeps profile ids stable
    resolve(client, a, django_capture_on_commit_callbacks)
    assert profile_of(a, "CRM-1") == pooja


def test_review_queue_confirm_and_reject(env, django_capture_on_commit_callbacks):
    a, _b, client, crm, web, orders = env
    add_sources(client, a, crm, web, orders)
    client.patch(url(a, "customers/settings"), {"auto_threshold": 0.99, "review_threshold": 0.5}, format="json")
    resolve(client, a, django_capture_on_commit_callbacks)
    queue = client.get(url(a, "customers/matches?status=pending_review")).json()["items"]
    rahul = [m for m in queue if {m["record_a"]["source_key"], m["record_b"]["source_key"]} == {"CRM-2", "WEB-2"}]
    assert rahul, queue
    mid = rahul[0]["id"]
    assert profile_of(a, "CRM-2") != profile_of(a, "WEB-2")
    resp = client.post(url(a, f"customers/matches/{mid}/decision"), {"decision": "confirm"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"] == "confirmed"
    assert profile_of(a, "CRM-2") == profile_of(a, "WEB-2")
    with a.tenant():
        assert AuditEvent.objects.filter(action="customers.match.confirmed", target_id=mid).exists()
    resp = client.post(url(a, f"customers/matches/{mid}/decision"), {"decision": "reject"}, format="json")
    assert resp.json()["status"] == "rejected"
    assert profile_of(a, "CRM-2") != profile_of(a, "WEB-2")
    resolve(client, a, django_capture_on_commit_callbacks)
    assert profile_of(a, "CRM-2") != profile_of(a, "WEB-2")
    with a.tenant():
        assert IdentityMatch.objects.get(id=mid).status == "rejected"
    assert client.post(url(a, f"customers/matches/{mid}/decision"), {"decision": "x"},
                       format="json").status_code == 400


def test_segments_rfm_and_rules(env, django_capture_on_commit_callbacks):
    a, _b, client, crm, web, orders = env
    add_sources(client, a, crm, web, orders)
    resolve(client, a, django_capture_on_commit_callbacks)
    rule = client.post(url(a, "customers/segments"), {"name": "Big spenders", "method": "rule", "definition": {
        "match": "all", "conditions": [{"field": "monetary", "op": ">=", "value": 1000}]}}, format="json")
    assert rule.status_code == 201, rule.content
    assert rule.json()["size"] == 1
    members = client.get(url(a, f"customers/segments/{rule.json()['id']}/members")).json()["items"]
    assert [m["id"] for m in members] == [str(profile_of(a, "CRM-1"))]
    bad = client.post(url(a, "customers/segments"), {"name": "Bad", "definition": {"conditions": [
        {"field": "ssn", "op": "eq", "value": 1}]}}, format="json")
    assert bad.status_code == 400
    rfm = client.post(url(a, "customers/segments"), {"method": "rfm"}, format="json")
    assert rfm.status_code == 201 and sum(s["size"] for s in rfm.json()["items"]) == 3
    refreshed = client.post(url(a, f"customers/segments/{rule.json()['id']}/refresh"))
    assert refreshed.status_code == 200 and refreshed.json()["size"] == 1
    assert client.delete(url(a, f"customers/segments/{rule.json()['id']}")).status_code == 204


def test_scoring_requires_a_model(env, django_capture_on_commit_callbacks):
    a, _b, client, crm, web, orders = env
    add_sources(client, a, crm, web, orders)
    resolve(client, a, django_capture_on_commit_callbacks)
    resp = client.post(url(a, "customers/scoring-runs"), {"score_type": "churn"}, format="json")
    assert resp.status_code == 409 and resp.json()["code"] == "ml_model_required"
    assert client.post(url(a, "customers/scoring-runs"), {"score_type": "nba"}, format="json").status_code == 400


def test_permissions_settings_and_policy(env, api_client_for):
    from apps.governance.models import ColumnPolicy

    a, _b, client, crm, _web, _orders = env
    assert client.get(url(a, "customers/settings")).json()["defaults"]["auto_threshold"] == 0.9
    assert client.patch(url(a, "customers/settings"), {"survivorship": {"email": "bogus"}},
                        format="json").status_code == 400
    analyst = api_client_for(a.users["analyst"])
    assert analyst.get(url(a, "customers")).status_code == 200
    assert analyst.get(url(a, "customers/matches")).status_code == 403
    assert analyst.post(url(a, "customers/resolve")).status_code == 403
    with a.tenant():
        ColumnPolicy.objects.create(dataset_id=crm["id"], column_name="email", principal_type="everyone",
                                    action="deny")
    engineer = api_client_for(a.user("data_engineer"))
    resp = engineer.post(url(a, "customers/sources"), {"dataset_id": crm["id"], "mapping": MAP_CRM}, format="json")
    assert resp.status_code == 400 and resp.json()["code"] == "column_not_allowed"
    assert resp.json()["excluded_by_policy"] == ["email"]


def test_cross_org_resources_are_404(env, django_capture_on_commit_callbacks, api_client_for):
    a, b, client, crm, web, orders = env
    src, _w, _o = add_sources(client, a, crm, web, orders)
    resolve(client, a, django_capture_on_commit_callbacks)
    pooja = profile_of(a, "CRM-1")
    other = api_client_for(b.owner)
    assert other.get(url(b, f"customers/{pooja}")).status_code == 404
    assert other.get(url(b, f"customers/{pooja}/timeline")).status_code == 404
    assert other.get(url(b, f"customers/sources/{src['id']}")).status_code == 404
    assert other.get(url(b, "customers")).json()["items"] == []
    resp = other.post(url(b, "customers/sources"), {"dataset_id": crm["id"], "mapping": MAP_CRM}, format="json")
    assert resp.status_code == 404
