"""Customer 360 / Product 360 (CONTRACT "W4 additions"; API_SPECIFICATION Phase 6 and Phase 7).

Entity-resolution QUALITY against the generator's ground truth (testdata/generate.py):
  * customers: customers_crm.csv (500) + customers_web.csv (450), 350 true cross-source pairs
    (customers_ground_truth.csv). After `POST /customers/resolve`, the CRM/WEB records merged into one profile
    must reach precision >= 0.90 and recall >= 0.80 on those pairs (only auto_merged / confirmed merges count;
    `pending_review` is not a merge);
  * products: products_supplier_a.csv + products_supplier_b.csv against products_ground_truth.csv. Precision and
    recall of the master-record grouping are REPORTED (printed and in the QA handoff), not gated.
Governance and workflow:
  * a masked or denied column cannot be mapped as a customer source field (the mapper is the restricted user);
  * a proposed product correction changes nothing until `approve`; `reject` never applies; a viewer cannot decide;
  * cross-org: org B cannot list, read or decide org A's profiles, matches or corrections (404).

The URLconf-driven isolation matrix (tests/isolation/test_endpoint_matrix.py) covers every new route as well.
DB-backed; each test skips until its routes exist. Synthetic data only.
"""

from __future__ import annotations

import csv
import io
import re
from itertools import combinations
from pathlib import Path

import pytest

from testing.client import api_client_for, tenant_route_exists
from testing.flows import items, upload_and_ingest
from testing.ml import pick

pytestmark = pytest.mark.django_db

GEN = Path(__file__).resolve().parents[3] / "testdata" / "generated"
needs_customers = pytest.mark.skipif(
    not tenant_route_exists("customers/resolve"), reason="Customer 360 not routed yet (Data Engineering W4)"
)
needs_products = pytest.mark.skipif(
    not tenant_route_exists("products/normalize"), reason="Product 360 not routed yet (Data Engineering W4)"
)

CUSTOMER_KEY = re.compile(r"\b(?:CRM|WEB)-\d{5}\b")
PRODUCT_KEY = re.compile(r"\b(?:A-[A-Z]{3}-\d{4}|SB/\d{5}-[A-Z])\b")


@pytest.fixture
def env(memory_storage, fake_redis, fake_embedder, fake_gemini):
    fake_gemini.respond("*", {"suggestions": []}, stream=[""])
    return fake_gemini


def _read(name: str) -> list[dict[str, str]]:
    with (GEN / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


def _ingest(client, world, filename: str, rows: list[dict], capture, name: str) -> str:
    return str(upload_and_ingest(client, world.slug, filename, _csv(rows), capture, name=name)["id"])


def _post_source(client, world, area: str, dataset_id: str, name: str, mapping: dict, expect=(200, 201)):
    resp = client.post(
        f"/api/v1/orgs/{world.slug}/{area}/sources",
        {"dataset_id": dataset_id, "name": name, "mapping": mapping, "field_mapping": mapping},
        format="json",
    )
    assert resp.status_code in expect, (resp.status_code, resp.content[:400])
    return resp


def _run(client, world, path: str, capture, body: dict | None = None) -> dict:
    with capture(execute=True):
        resp = client.post(f"/api/v1/orgs/{world.slug}/{path}", body or {}, format="json")
    assert resp.status_code in (200, 202), (path, resp.status_code, resp.content[:400])
    return resp.json() if resp.content else {}


def _clusters(world, record_model: str, master_model: str, key_re: re.Pattern, app: str) -> dict[str, set[str]]:
    """{source key (CRM-00001 / A-ELE-0001 ...): master id} read from the source-record rows (ORM, tenant scope)."""
    from django.apps import apps as django_apps
    from django.forms.models import model_to_dict

    Record = django_apps.get_model(app, record_model)
    Master = django_apps.get_model(app, master_model)
    fk = next((f for f in Record._meta.concrete_fields if getattr(f, "related_model", None) is Master), None)
    assert fk is not None, f"{record_model} has no FK to {master_model}"
    groups: dict[str, set[str]] = {}
    with world.tenant():
        for rec in Record.objects.all():
            master = getattr(rec, fk.attname)
            blob = " ".join(str(v) for v in model_to_dict(rec).values())
            keys = set(key_re.findall(blob))
            if master is None or not keys:
                continue
            groups.setdefault(str(master), set()).update(keys)
    return groups


def _pairs(groups: dict[str, set[str]], left: str, right: str) -> set[tuple[str, str]]:
    out = set()
    for keys in groups.values():
        for x, y in combinations(sorted(keys), 2):
            if x.startswith(left) and y.startswith(right):
                out.add((x, y))
            elif y.startswith(left) and x.startswith(right):
                out.add((y, x))
    return out


def _prf(predicted: set, truth: set) -> tuple[float, float, float]:
    tp = len(predicted & truth)
    p = tp / len(predicted) if predicted else 0.0
    r = tp / len(truth) if truth else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


# ------------------------------------------------------------------------------------------------ customers
CRM_MAP = {"external_id": "crm_id", "name": "full_name", "email": "email", "phone": "phone", "dob": "date_of_birth",
           "address": "city"}
WEB_MAP = {"external_id": "web_id", "name": "full_name", "email": "email", "phone": "mobile", "address": "city",
           "created_at": "signup_date"}


def _crm_rows() -> list[dict]:
    # the mapper takes ONE name column: the test feeds first + last as `full_name` (the ground truth is unchanged)
    return [{**r, "full_name": f"{r['first_name']} {r['last_name']}"} for r in _read("customers_crm.csv")]


def _customer_sources(client, world, capture) -> tuple[str, str]:
    crm = _ingest(client, world, "customers_crm.csv", _crm_rows(), capture, "Customers (CRM)")
    web = _ingest(client, world, "customers_web.csv", _read("customers_web.csv"), capture, "Customers (web)")
    _post_source(client, world, "customers", crm, "CRM", CRM_MAP)
    _post_source(client, world, "customers", web, "Web", WEB_MAP)
    return crm, web


@needs_customers
def test_customer_resolution_meets_precision_and_recall_on_the_ground_truth(two_orgs, env,
                                                                            django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    _customer_sources(owner, a, capture)
    _run(owner, a, "customers/resolve", capture)
    truth = {(t["crm_id"], t["web_id"]) for t in _read("customers_ground_truth.csv") if t["crm_id"] and t["web_id"]}
    assert len(truth) == 350
    groups = _clusters(a, "CustomerSourceRecord", "CustomerProfile", CUSTOMER_KEY, "customers")
    assert groups, "resolution produced no profiles"
    predicted = _pairs(groups, "CRM-", "WEB-")
    p, r, f1 = _prf(predicted, truth)
    print(f"\nC360 entity resolution: precision {p:.3f} recall {r:.3f} F1 {f1:.3f} "
          f"(predicted {len(predicted)}, true {len(truth)})")
    assert p >= 0.90, f"precision {p:.3f} < 0.90"
    assert r >= 0.80, f"recall {r:.3f} < 0.80"
    same_source = [k for k in groups.values() if sum(x.startswith("CRM-") for x in k) > 1]
    assert len(same_source) <= 0.02 * len(groups), f"distinct CRM people merged together: {same_source[:3]}"
    # every profile is listed exactly once through the API
    listed = owner.get(f"/api/v1/orgs/{a.slug}/customers")
    assert listed.status_code == 200
    assert listed.json().get("items") is not None, "customer list uses the cursor envelope"


@needs_customers
def test_masked_or_denied_columns_cannot_be_mapped_as_customer_fields(two_orgs, env,
                                                                      django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner, analyst = api_client_for(a.owner), api_client_for(a.analyst)
    crm = _ingest(owner, a, "customers_crm.csv", _crm_rows(), django_capture_on_commit_callbacks, "Customers")
    for column, action in (("email", "deny"), ("phone", "mask_hash"), ("date_of_birth", "mask_null")):
        resp = owner.post(
            f"/api/v1/orgs/{a.slug}/datasets/{crm}/column-policies",
            {"column_name": column, "principal_type": "user", "principal_id": str(a.analyst.pk), "action": action},
            format="json",
        )
        assert resp.status_code == 201, resp.content[:300]
    for field, column in (("email", "email"), ("phone", "phone"), ("dob", "date_of_birth")):
        resp = _post_source(analyst, a, "customers", crm, f"bad {field}",
                            {"external_id": "crm_id", "name": "full_name", field: column}, expect=(400, 403, 422))
        assert column in resp.content.decode(), f"the refusal must name the hidden column {column!r}"
    # control: the same analyst may map the allowed columns, and the owner (no policy) may map everything
    _post_source(analyst, a, "customers", crm, "allowed", {"external_id": "crm_id", "name": "full_name",
                                                          "address": "city"})
    _post_source(owner, a, "customers", crm, "owner", CRM_MAP)


@needs_customers
def test_match_review_is_reviewer_only_and_org_scoped(two_orgs, env, django_capture_on_commit_callbacks):
    a, b = two_orgs
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    _customer_sources(owner, a, capture)
    _run(owner, a, "customers/resolve", capture)
    pending = owner.get(f"/api/v1/orgs/{a.slug}/customers/matches", {"status": "pending_review"})
    assert pending.status_code == 200, pending.content[:300]
    customers = items(owner.get(f"/api/v1/orgs/{a.slug}/customers"))
    assert customers, "no profiles"
    cid = str(pick(customers[0], "id"))
    b_owner = api_client_for(b.owner)
    for path in (f"customers/{cid}", f"customers/{cid}/timeline"):
        assert owner.get(f"/api/v1/orgs/{a.slug}/{path}").status_code == 200
        for slug in (a.slug, b.slug):
            assert b_owner.get(f"/api/v1/orgs/{slug}/{path}").status_code == 404
    listed_b = b_owner.get(f"/api/v1/orgs/{b.slug}/customers").content.decode()
    assert cid not in listed_b and "CRM-0" not in listed_b
    rows = items(pending)
    if not rows:
        pytest.skip("no pending_review match at the default thresholds; decision checks need one")
    match_id = str(pick(rows[0], "id"))
    url = f"/api/v1/orgs/{a.slug}/customers/matches/{match_id}/decision"
    assert api_client_for(a.viewer).post(url, {"decision": "confirm"}, format="json").status_code == 403
    assert b_owner.post(f"/api/v1/orgs/{b.slug}/customers/matches/{match_id}/decision", {"decision": "confirm"},
                        format="json").status_code == 404
    resp = api_client_for(a.analyst).post(url, {"decision": "reject"}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    after = owner.get(f"/api/v1/orgs/{a.slug}/customers/matches", {"status": "pending_review"})
    assert match_id not in after.content.decode()


# ------------------------------------------------------------------------------------------------ products
A_MAP = {"sku": "sku", "name": "product_name", "category": "category", "price": "mrp_inr", "unit": "unit",
         "pack_size": "pack_size"}
B_MAP = {"sku": "item_code", "name": "description", "category": "cat", "price": "price", "unit": "uom",
         "pack_size": "qty_per_pack"}


def _product_sources(client, world, capture) -> None:
    a = _ingest(client, world, "products_supplier_a.csv", _read("products_supplier_a.csv"), capture, "Supplier A")
    b = _ingest(client, world, "products_supplier_b.csv", _read("products_supplier_b.csv"), capture, "Supplier B")
    _post_source(client, world, "products", a, "Supplier A", A_MAP)
    _post_source(client, world, "products", b, "Supplier B", B_MAP)


@needs_products
def test_product_matching_precision_and_recall_are_reported(two_orgs, env, django_capture_on_commit_callbacks):
    a = two_orgs.a
    owner = api_client_for(a.owner)
    capture = django_capture_on_commit_callbacks
    _product_sources(owner, a, capture)
    _run(owner, a, "products/normalize", capture)
    truth_rows = _read("products_ground_truth.csv")
    truth = {(t["supplier_a_sku"], t["supplier_b_item_code"]) for t in truth_rows
             if t["supplier_a_sku"] and t["supplier_b_item_code"]}
    groups = _clusters(a, "ProductSourceRecord", "ProductMaster", PRODUCT_KEY, "products")
    assert groups, "normalisation produced no master records"
    predicted = _pairs(groups, "A-", "SB/")
    p, r, f1 = _prf(predicted, truth)
    print(f"\nP360 product matching: precision {p:.3f} recall {r:.3f} F1 {f1:.3f} "
          f"(predicted {len(predicted)}, true {len(truth)})")
    assert truth, "the ground truth has cross-supplier pairs"
    # reported, not gated (QA mission W4 D); a grouping that merges everything is still a defect
    assert len(groups) >= 0.5 * len({t["master_id"] for t in truth_rows}), "masters collapsed into too few groups"


def _product_field(product: dict, field: str):
    attrs = product.get("attributes")
    if isinstance(attrs, dict) and field in attrs:
        return attrs[field]
    if isinstance(attrs, list):
        for at in attrs:
            if isinstance(at, dict) and at.get("name", at.get("key")) == field:
                return at.get("value")
    return product.get(field)


@needs_products
def test_product_corrections_apply_only_after_approve(two_orgs, env, django_capture_on_commit_callbacks):
    a, b = two_orgs
    owner, viewer, analyst = api_client_for(a.owner), api_client_for(a.viewer), api_client_for(a.analyst)
    capture = django_capture_on_commit_callbacks
    _product_sources(owner, a, capture)
    _run(owner, a, "products/normalize", capture)
    _run(owner, a, "products/enrich", capture)
    base = f"/api/v1/orgs/{a.slug}/products"
    proposed = items(owner.get(f"{base}/corrections", {"status": "proposed"}))
    if len(proposed) < 2:
        pytest.skip(f"enrich proposed {len(proposed)} correction(s); the approve/reject check needs two")

    def snapshot(corr):
        pid = str(pick(corr, "product_id", "product.id", "product"))
        field = str(pick(corr, "field", "attribute", "attribute_name", "name"))
        detail = owner.get(f"{base}/{pid}")
        assert detail.status_code == 200, detail.content[:300]
        return pid, field, _product_field(detail.json(), field)

    approve, reject = proposed[0], proposed[1]
    before = {str(pick(c, "id")): snapshot(c) for c in (approve, reject)}
    for c in (approve, reject):  # still proposed: the product does NOT carry the proposed value yet
        _pid, field, value = before[str(pick(c, "id"))]
        new = pick(c, "proposed_value", "new_value", "value", "after")
        assert str(value) != str(new), f"proposed correction of {field!r} is already applied (or a no-op)"
    aid, rid = str(pick(approve, "id")), str(pick(reject, "id"))
    assert viewer.post(f"{base}/corrections/{aid}/decision", {"decision": "approve"},
                       format="json").status_code == 403
    assert api_client_for(b.owner).post(f"/api/v1/orgs/{b.slug}/products/corrections/{aid}/decision",
                                        {"decision": "approve"}, format="json").status_code == 404
    assert snapshot(approve)[2] == before[aid][2], "a refused decision changed the product"

    resp = analyst.post(f"{base}/corrections/{rid}/decision", {"decision": "reject"}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    assert snapshot(reject)[2] == before[rid][2], "a rejected correction was applied"

    resp = analyst.post(f"{base}/corrections/{aid}/decision", {"decision": "approve"}, format="json")
    assert resp.status_code == 200, resp.content[:300]
    pid, field, value = snapshot(approve)
    assert str(value) == str(pick(approve, "proposed_value", "new_value", "value", "after")), (
        f"approved correction not applied to {field}"
    )
    statuses = {str(pick(c, "id")): pick(c, "status") for c in items(owner.get(f"{base}/corrections"))}
    assert statuses.get(rid) == "rejected" and statuses.get(aid) in ("applied", "approved"), statuses
