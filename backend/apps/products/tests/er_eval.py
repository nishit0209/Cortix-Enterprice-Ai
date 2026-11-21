"""Entity-resolution evaluation on testdata/generated (QA's synthetic suppliers / CRM+web with ground truth).

Used by the unit tests of apps.customers and apps.products; also runnable:
    python -m apps.products.tests.er_eval
"""

from __future__ import annotations

import csv
from pathlib import Path

GENERATED = Path(__file__).resolve().parents[4] / "testdata" / "generated"


def _rows(name: str) -> list[dict]:
    with (GENERATED / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def product_evaluation() -> dict:
    from apps.products import matching as M

    raws: dict[str, dict] = {}
    for r in _rows("products_supplier_a.csv"):
        raws[f"a:{r['sku']}"] = {"sku": r["sku"], "name": r["product_name"], "category": r["category"],
                                 "unit": r["unit"], "pack_size": r["pack_size"], "price": r["mrp_inr"]}
    for r in _rows("products_supplier_b.csv"):
        raws[f"b:{r['item_code']}"] = {"sku": r["item_code"], "name": r["description"], "category": r["cat"],
                                       "unit": r["uom"], "pack_size": r["qty_per_pack"], "price": r["price"]}
    known = M.known_brands_of(raws.values())
    records = {k: M.normalize_record(v, known_brands=known) for k, v in raws.items()}
    result = M.resolve(records)
    truth = []
    for t in _rows("products_ground_truth.csv"):
        ids = [i for i in (f"a:{t['supplier_a_sku']}" if t["supplier_a_sku"] else "",
                           f"b:{t['supplier_b_item_code']}" if t["supplier_b_item_code"] else "") if i]
        truth.append(ids)
    return {**M.pairwise_metrics(result.clusters, truth), "stats": result.stats}


def catalog_evaluation() -> dict:
    """products.csv: duplicate listings (spaced / compact / legacy / unit-converted SKUs) must merge, pack-size
    variant families must NOT (commerce_meta.json holds the truth)."""
    import json

    from apps.products import matching as M

    raws = {r["product_id"]: {"sku": r["sku"], "name": r["product_name"], "brand": r["brand"],
                              "category": r["category"], "unit": r["unit"], "pack_size": r["pack_size"],
                              "price": r["list_price_inr"]} for r in _rows("products.csv")}
    meta = json.loads((GENERATED / "commerce_meta.json").read_text(encoding="utf-8"))
    known = M.known_brands_of(raws.values())
    result = M.resolve({k: M.normalize_record(v, known_brands=known) for k, v in raws.items()})
    truth = [[a, b] for a, b, _style in meta["duplicate_pairs"]]
    cluster_of = {rid: i for i, c in enumerate(result.clusters) for rid in c}
    variant_merges = sum(1 for fam in meta["variant_families"] for i, a in enumerate(fam) for b in fam[i + 1:]
                         if cluster_of.get(a) == cluster_of.get(b))
    return {**M.pairwise_metrics(result.clusters, truth), "variant_pairs_merged": variant_merges,
            "stats": result.stats}


def customer_evaluation() -> dict:
    from apps.customers import matching as M

    raws: dict[str, dict] = {}
    for r in _rows("customers_crm.csv"):
        raws[r["crm_id"]] = {"external_id": r["crm_id"], "first_name": r["first_name"], "last_name": r["last_name"],
                             "email": r["email"], "phone": r["phone"], "address": r["city"],
                             "dob": r["date_of_birth"]}
    for r in _rows("customers_web.csv"):
        raws[r["web_id"]] = {"external_id": r["web_id"], "name": r["full_name"], "email": r["email"],
                             "phone": r["mobile"], "address": r["city"], "created_at": r["signup_date"]}
    records = {k: M.normalize_record(v) for k, v in raws.items()}
    result = M.resolve(records)
    truth = [[i for i in (t["crm_id"], t["web_id"]) if i] for t in _rows("customers_ground_truth.csv")]
    return {**M.pairwise_metrics(result.clusters, truth), "stats": result.stats}


if __name__ == "__main__":  # pragma: no cover
    import django

    django.setup()
    print("products", product_evaluation())
    print("customers", customer_evaluation())
