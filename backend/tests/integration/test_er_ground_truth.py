"""Entity-resolution ground truth self-checks (no DB): the truth files agree with the source CSVs, the pair/metric
helpers of test_customer_product_360 are right, and the C360 gate (precision >= 0.90, recall >= 0.80) is
achievable: a 10-line reference resolver (normalised phone OR normalised email) clears it on the same data.
"""

from __future__ import annotations

import re

from tests.integration.test_customer_product_360 import (
    CUSTOMER_KEY,
    PRODUCT_KEY,
    _crm_rows,
    _pairs,
    _prf,
    _read,
)


def test_customer_truth_matches_the_source_files():
    crm = {r["crm_id"] for r in _read("customers_crm.csv")}
    web = {r["web_id"] for r in _read("customers_web.csv")}
    truth = _read("customers_ground_truth.csv")
    assert {t["crm_id"] for t in truth if t["crm_id"]} == crm
    assert {t["web_id"] for t in truth if t["web_id"]} == web
    assert sum(1 for t in truth if t["crm_id"] and t["web_id"]) == 350
    assert all(CUSTOMER_KEY.fullmatch(k) for k in crm | web)
    assert all(r["full_name"] == f"{r['first_name']} {r['last_name']}" for r in _crm_rows())


def test_product_truth_matches_the_source_files():
    a = {r["sku"] for r in _read("products_supplier_a.csv")}
    b = {r["item_code"] for r in _read("products_supplier_b.csv")}
    truth = _read("products_ground_truth.csv")
    assert {t["supplier_a_sku"] for t in truth if t["supplier_a_sku"]} <= a
    assert {t["supplier_b_item_code"] for t in truth if t["supplier_b_item_code"]} <= b
    assert any(t["supplier_a_sku"] and t["supplier_b_item_code"] for t in truth)
    assert all(PRODUCT_KEY.fullmatch(k) for k in a | b), [k for k in a | b if not PRODUCT_KEY.fullmatch(k)][:5]


def test_pair_and_metric_helpers():
    groups = {"m1": {"CRM-00001", "WEB-00002", "CRM-00003"}, "m2": {"WEB-00009"}}
    assert _pairs(groups, "CRM-", "WEB-") == {("CRM-00001", "WEB-00002"), ("CRM-00003", "WEB-00002")}
    p, r, f1 = _prf({("a", "b"), ("c", "d")}, {("a", "b"), ("e", "f"), ("g", "h")})
    assert (p, round(r, 4), round(f1, 4)) == (0.5, 0.3333, 0.4)
    assert _prf(set(), {("a", "b")}) == (0.0, 0.0, 0.0)


def _phone(p: str) -> str:
    digits = re.sub(r"\D", "", p)
    return digits[-10:]


def _email(e: str) -> str:
    local, _, domain = e.strip().lower().partition("@")
    return f"{local.replace('.', '')}@{domain}"


def test_the_c360_gate_is_achievable_with_a_reference_resolver():
    crm, web = _read("customers_crm.csv"), _read("customers_web.csv")
    by_phone = {_phone(r["phone"]): r["crm_id"] for r in crm}
    by_email = {_email(r["email"]): r["crm_id"] for r in crm}
    predicted = set()
    for w in web:
        hit = by_phone.get(_phone(w["mobile"])) or by_email.get(_email(w["email"]))
        if hit:
            predicted.add((hit, w["web_id"]))
    truth = {(t["crm_id"], t["web_id"]) for t in _read("customers_ground_truth.csv") if t["crm_id"] and t["web_id"]}
    p, r, _f1 = _prf(predicted, truth)
    assert p >= 0.90 and r >= 0.80, (p, r)
