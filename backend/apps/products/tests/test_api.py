"""Product 360 API (DB-backed: real Postgres under RLS + nexora_wh; storage faked; tasks run eagerly)."""

import pytest

from apps.core.models import AuditEvent
from apps.datasets.tests.helpers import csv_bytes, ingest
from apps.products.models import ProductCorrection, ProductMaster, ProductMatch, ProductSourceRecord

pytestmark = pytest.mark.django_db

SUPPLIER_A = [
    ["sku", "product_name", "category", "unit", "pack_size", "price"],
    ["A-ELE-0001", "Sahyadri USB-C Cable 1pc", "Electronics", "pc", "1", "499.00"],
    ["A-GRO-0002", "Konark Masala Chai 500g", "Grocery", "g", "500", "450.00"],
    ["A-GRO-0003", "Konark Masala Chai 1kg", "Grocery", "kg", "1", "860.00"],
    ["A-HOM-0004", "Nilgiri Cotton Bedsheet Red 1pc", "Home", "pc", "1", "1299.00"],
    ["A-BEA-0005", "Nilgiri Herbal Shampoo 340ml", "Beauty", "ml", "340", "320.00"],
    ["A-ELE-0006", "Konark USB-C Cable 1pc", "", "pc", "1", "450.00"],
]
SUPPLIER_B = [
    ["item_code", "description", "cat", "qty_per_pack", "uom", "price"],
    ["SB/00001-Y", "USB-C CABLE (Sahyadri) 1 PC", "electronics", "1", "PC", "505.00"],
    ["SB/00002-Z", "MASALA CHAI (Konark) 0.5 KG", "grocery", "0.5", "KG", "455.00"],
    ["SB/00003-X", "HERBAL SHAMPO (Nilgiri) 0.34 L", "beauty", "0.34", "L", "480.00"],
]
MAP_A = {"sku": "sku", "name": "product_name", "category": "category", "unit": "unit", "pack_size": "pack_size",
         "price": "price"}
MAP_B = {"sku": "item_code", "name": "description", "category": "cat", "unit": "uom", "pack_size": "qty_per_pack",
         "price": "price"}


def url(world, path=""):
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def env(two_orgs, api_client_for, fake_redis, fake_storage, django_capture_on_commit_callbacks):
    a, b = two_orgs
    client = api_client_for(a.owner)
    _r, ds_a = ingest(client, a, "supplier_a.csv", csv_bytes(SUPPLIER_A), django_capture_on_commit_callbacks,
                      name="Supplier A")
    _r, ds_b = ingest(client, a, "supplier_b.csv", csv_bytes(SUPPLIER_B), django_capture_on_commit_callbacks,
                      name="Supplier B")
    assert ds_a["status"] == ds_b["status"] == "ready"
    return a, b, client, ds_a, ds_b


def add_sources(client, a, ds_a, ds_b):
    ra = client.post(url(a, "products/sources"), {"dataset_id": ds_a["id"], "mapping": MAP_A, "priority": 10},
                     format="json")
    assert ra.status_code == 201, ra.content
    rb = client.post(url(a, "products/sources"), {"dataset_id": ds_b["id"], "mapping": MAP_B, "priority": 20},
                     format="json")
    assert rb.status_code == 201, rb.content
    return ra.json(), rb.json()


def normalize(client, a, capture):
    with capture(execute=True):
        resp = client.post(url(a, "products/normalize"))
    assert resp.status_code == 202, resp.content
    run = client.get(url(a, f"products/runs/{resp.json()['run_id']}")).json()
    assert run["status"] == "succeeded", run
    return run


def test_sources_normalize_match_and_masters(env, django_capture_on_commit_callbacks, captured_notifications):
    a, _b, client, ds_a, ds_b = env
    assert client.post(url(a, "products/normalize")).json()["code"] == "product_sources_required"
    src_a, _src_b = add_sources(client, a, ds_a, ds_b)
    assert src_a["source_system"] == "supplier-a" and src_a["mapping"]["sku"] == "sku"
    dup = client.post(url(a, "products/sources"), {"dataset_id": ds_a["id"], "mapping": MAP_A}, format="json")
    assert dup.status_code == 409 and dup.json()["code"] == "product_source_exists"
    bad = client.post(url(a, "products/sources"), {"dataset_id": ds_a["id"], "mapping": {"name": "nope"},
                                                   "name": "Other"}, format="json")
    assert bad.status_code == 400

    run = normalize(client, a, django_capture_on_commit_callbacks)
    assert run["stats"]["records"] == 9
    with a.tenant():
        assert ProductSourceRecord.objects.count() == 9
        cable = ProductMaster.objects.get(sku_normalized="AELE0001")
        assert cable.record_count == 2 and sorted(cable.sources) == ["supplier-a", "supplier-b"]
        chai_500 = ProductSourceRecord.objects.get(source_key="sku:A-GRO-0002").master
        chai_1kg = ProductSourceRecord.objects.get(source_key="sku:A-GRO-0003").master
        assert chai_500.id != chai_1kg.id  # pack-size variants stay separate products
        assert ProductSourceRecord.objects.get(source_key="sku:SB/00002-Z").master_id == chai_500.id
    listed = client.get(url(a, "products")).json()["items"]
    assert len(listed) == ProductMaster.unscoped.filter(org_id=a.org.id).count()
    detail = client.get(url(a, f"products/{cable.id}")).json()
    assert detail["brand"] == "Sahyadri" and detail["quality_detail"]["overall"] > 50
    assert len(detail["records"]) == 2 and detail["matches"][0]["status"] == "auto_merged"
    attrs = client.get(url(a, f"products/{chai_500.id}/attributes")).json()["items"]
    assert isinstance(attrs, list)
    bedsheet = client.get(url(a, "products?search=bedsheet")).json()["items"][0]
    bed_attrs = {x["key"]: x for x in client.get(url(a, f"products/{bedsheet['id']}/attributes")).json()["items"]}
    assert bed_attrs["colour"]["value"] == "red" and bed_attrs["material"]["origin"] == "extracted"
    assert captured_notifications and captured_notifications[-1]["kind"] == "products.run_finished"

    # re-running keeps master ids stable
    normalize(client, a, django_capture_on_commit_callbacks)
    with a.tenant():
        assert ProductMaster.objects.filter(id=cable.id).exists()


def test_review_queue_confirm_and_reject(env, django_capture_on_commit_callbacks):
    a, _b, client, ds_a, ds_b = env
    add_sources(client, a, ds_a, ds_b)
    normalize(client, a, django_capture_on_commit_callbacks)
    queue = client.get(url(a, "products/matches?status=pending_review")).json()["items"]
    shampoo = [m for m in queue if "SHAMPO" in str(m["record_b"]["attributes"]) or
               "SHAMPO" in str(m["record_a"]["attributes"])]
    assert shampoo, queue  # typo + 33 % price gap -> review, not auto
    mid = shampoo[0]["id"]
    with a.tenant():
        m = ProductMatch.objects.get(id=mid)
        ra, rb = ProductSourceRecord.objects.get(id=m.record_a_id), ProductSourceRecord.objects.get(id=m.record_b_id)
        assert ra.master_id != rb.master_id
    bad = client.post(url(a, f"products/matches/{mid}/decision"), {"decision": "maybe"}, format="json")
    assert bad.status_code == 400
    resp = client.post(url(a, f"products/matches/{mid}/decision"), {"decision": "confirm"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"] == "confirmed"
    with a.tenant():
        ra.refresh_from_db(), rb.refresh_from_db()
        assert ra.master_id == rb.master_id
        assert AuditEvent.objects.filter(action="products.match.confirmed", target_id=mid).exists()
    resp = client.post(url(a, f"products/matches/{mid}/decision"), {"decision": "reject"}, format="json")
    assert resp.json()["status"] == "rejected"
    with a.tenant():
        ra.refresh_from_db(), rb.refresh_from_db()
        assert ra.master_id != rb.master_id
    normalize(client, a, django_capture_on_commit_callbacks)  # the rejection survives a re-run
    with a.tenant():
        ra.refresh_from_db(), rb.refresh_from_db()
        assert ra.master_id != rb.master_id and ProductMatch.objects.get(id=mid).status == "rejected"


def test_corrections_apply_only_on_approve_and_enrich(env, django_capture_on_commit_callbacks):
    a, _b, client, ds_a, ds_b = env
    assert client.post(url(a, "products/enrich")).json()["code"] == "product_masters_required"
    add_sources(client, a, ds_a, ds_b)
    normalize(client, a, django_capture_on_commit_callbacks)
    with a.tenant():
        master = ProductMaster.objects.get(sku_normalized="AELE0001")
    resp = client.post(url(a, "products/corrections"), {"master_id": str(master.id), "field": "name",
                                                        "value": "Sahyadri USB-C Cable (1 m)"}, format="json")
    assert resp.status_code == 201 and resp.json()["status"] == "proposed"
    cid = resp.json()["id"]
    assert client.get(url(a, f"products/{master.id}")).json()["name"] != "Sahyadri USB-C Cable (1 m)"
    bad = client.post(url(a, "products/corrections"), {"master_id": str(master.id), "field": "price", "value": -4},
                      format="json")
    assert bad.status_code == 400
    resp = client.post(url(a, f"products/corrections/{cid}/decision"), {"decision": "approve"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"] == "applied" and resp.json()["applied_at"]
    assert client.get(url(a, f"products/{master.id}")).json()["name"] == "Sahyadri USB-C Cable (1 m)"
    again = client.post(url(a, f"products/corrections/{cid}/decision"), {"decision": "reject"}, format="json")
    assert again.status_code == 409
    normalize(client, a, django_capture_on_commit_callbacks)  # applied corrections survive re-runs
    assert client.get(url(a, f"products/{master.id}")).json()["name"] == "Sahyadri USB-C Cable (1 m)"

    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(url(a, "products/enrich"))
    assert resp.status_code == 202
    run = client.get(url(a, f"products/runs/{resp.json()['run_id']}")).json()
    assert run["status"] == "succeeded" and run["stats"]["proposed"] > 0 and run["stats"]["mode"] == "local"
    proposed = client.get(url(a, "products/corrections?status=proposed")).json()["items"]
    assert proposed and all(c["origin"] == "rule" for c in proposed)
    with a.tenant():
        target = ProductCorrection.objects.get(id=proposed[0]["id"])
        before = dict(target.master.values)
    rej = client.post(url(a, f"products/corrections/{target.id}/decision"), {"decision": "reject"}, format="json")
    assert rej.json()["status"] == "rejected"
    with a.tenant():
        target.master.refresh_from_db()
        assert target.master.values == before
        assert AuditEvent.objects.filter(action="products.correction.reject", target_id=str(target.id)).exists()


def test_search_text_fallback_and_semantic(env, django_capture_on_commit_callbacks, fake_embedder):
    a, _b, client, ds_a, ds_b = env
    add_sources(client, a, ds_a, ds_b)
    run = normalize(client, a, django_capture_on_commit_callbacks)
    assert run["stats"]["search_mode"] == "semantic" and run["stats"]["embedded"] > 0
    body = client.post(url(a, "products/search"), {"query": "usb cable", "k": 3}, format="json").json()
    assert body["search_mode"] == "semantic" and body["items"]
    assert any("Cable" in i["name"] for i in body["items"])
    assert client.post(url(a, "products/search"), {"query": ""}, format="json").status_code == 400


def test_search_without_model_uses_text(env, django_capture_on_commit_callbacks, monkeypatch):
    monkeypatch.setattr("apps.ai.embeddings.embedding_available", lambda: False)
    a, _b, client, ds_a, ds_b = env
    add_sources(client, a, ds_a, ds_b)
    normalize(client, a, django_capture_on_commit_callbacks)
    body = client.post(url(a, "products/search"), {"query": "masala chai"}, format="json").json()
    assert body["search_mode"] == "text" and body["reason"] in ("model_unavailable", "not_indexed")
    assert body["items"][0]["name"].lower().startswith("konark masala chai")


def test_settings_and_permissions(env, api_client_for):
    a, _b, client, ds_a, _ds_b = env
    resp = client.patch(url(a, "products/settings"), {"auto_threshold": 0.95, "review_threshold": 0.8,
                                                      "brand_aliases": {"Sahyadri Ltd": "Sahyadri"}}, format="json")
    assert resp.status_code == 200 and resp.json()["auto_threshold"] == 0.95
    assert client.patch(url(a, "products/settings"), {"auto_threshold": 0.5, "review_threshold": 0.8},
                        format="json").status_code == 400
    analyst = api_client_for(a.users["analyst"])
    assert analyst.get(url(a, "products")).status_code == 200
    assert analyst.post(url(a, "products/sources"), {"dataset_id": ds_a["id"], "mapping": MAP_A},
                        format="json").status_code == 403
    assert analyst.get(url(a, "products/matches")).status_code == 403
    assert analyst.post(url(a, "products/normalize")).status_code == 403


def test_masked_columns_cannot_be_mapped(env, api_client_for):
    from apps.governance.models import ColumnPolicy

    a, _b, client, ds_a, _ds_b = env
    with a.tenant():
        ColumnPolicy.objects.create(dataset_id=ds_a["id"], column_name="price", principal_type="everyone",
                                    action="deny")
    engineer = api_client_for(a.user("data_engineer"))
    resp = engineer.post(url(a, "products/sources"), {"dataset_id": ds_a["id"], "mapping": MAP_A}, format="json")
    assert resp.status_code == 400 and resp.json()["code"] == "column_not_allowed"
    assert resp.json()["excluded_by_policy"] == ["price"]


def test_cross_org_resources_are_404(env, django_capture_on_commit_callbacks, api_client_for):
    a, b, client, ds_a, ds_b = env
    src, _ = add_sources(client, a, ds_a, ds_b)
    normalize(client, a, django_capture_on_commit_callbacks)
    with a.tenant():
        master = ProductMaster.objects.first()
    other = api_client_for(b.owner)
    assert other.get(url(b, f"products/{master.id}")).status_code == 404
    assert other.get(url(b, f"products/sources/{src['id']}")).status_code == 404
    assert other.get(url(b, "products")).json()["items"] == []
    resp = other.post(url(b, "products/sources"), {"dataset_id": ds_a["id"], "mapping": MAP_A}, format="json")
    assert resp.status_code == 404
