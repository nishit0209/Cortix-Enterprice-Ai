"""Product 360 pure functions (no DB): SKU / unit / brand / attribute parsing, matching, quality, enrichment rules,
and the entity-resolution evaluation on testdata/generated."""

import pytest

from apps.products import matching as M

from .er_eval import catalog_evaluation, product_evaluation


@pytest.mark.parametrize("raw,sku,stem", [
    ("nil-basric-5kg", "NILBASRIC5KG", "NILBASRIC"),
    ("NIL BASRIC 5KG", "NILBASRIC5KG", "NILBASRIC"),
    ("OLD-NIL-BASRIC-5KG", "NILBASRIC5KG", "NILBASRIC"),
    ("TSH-COT-XL", "TSHCOTXL", "TSHCOT"),
    ("", None, None),
])
def test_sku_normalization_and_stem(raw, sku, stem):
    assert M.normalize_sku(raw) == sku
    assert M.sku_stem(raw) == stem


def test_sku_shape_separates_schemes():
    assert M.sku_shape("AELE0098") == M.sku_shape("AGRO0002")
    assert M.sku_shape("AELE0098") != M.sku_shape("SB00033Y")


@pytest.mark.parametrize("text,expected", [
    ("Masala Chai 500g", (500.0, "g")),
    ("Shampoo 0.5 L", (500.0, "ml")),
    ("Rice 1,5 kg", (1500.0, "g")),
    ("Biscuits 2 x 100 g", (200.0, "g")),
    ("Cable 1 pc", (1.0, "pc")),
    ("No quantity here", None),
])
def test_quantity_parsing_to_base_units(text, expected):
    got = M.parse_quantity(text)
    assert (None if got is None else (round(got[0], 3), got[1])) == expected


def test_brand_canonicalization_and_inference():
    assert M.canonical_brand("Coca Cola", {"coca cola": "Coca-Cola"}) == M.canonical_brand("coca-cola")
    assert M.parenthesised_brand("SPORTS SHOES (Sahyadri) 1 PAIR") == "Sahyadri"
    assert M.infer_brand("Nilgiri LED Bulb 9W", {"Nilgiri", "Konark"}) == "nilgiri"


def test_attribute_extraction():
    attrs = M.extract_attributes("Konark Cotton T-Shirt Red Size XL pack of 3")
    assert attrs.get("material") == "cotton" and attrs.get("colour") == "red"
    assert attrs.get("size") == "XL" and attrs.get("pack_count") == "3"
    assert M.extract_attributes("Nilgiri Power Bank 10000mAh").get("capacity", "").lower().startswith("10000")


def _norm(**raw):
    return M.normalize_record(raw, known_brands={"Sahyadri", "Konark", "Nilgiri"})


def test_duplicate_across_suppliers_scores_high():
    a = _norm(sku="A-ELE-0001", name="Sahyadri USB-C Cable 1pc", category="Electronics", price="499")
    b = _norm(sku="SB/00001-Y", name="USB-C CABLE (Sahyadri) 1 PC", category="electronics", price="505")
    s = M.score_pair(a, b)
    assert s.features["sku"] is None and s.score >= M.DEFAULT_AUTO


def test_pack_size_variant_is_never_merged():
    a = _norm(sku="NIL-BASRIC-5KG", name="Nilgiri Basmati Rice 5 kg", brand="Nilgiri", price="554")
    b = _norm(sku="NIL-BASRIC-10KG", name="Nilgiri Basmati Rice 10 kg", brand="Nilgiri", price="1498")
    s = M.score_pair(a, b)
    assert "different_pack_size" in s.rules and M.decide(s.score) is None


def test_same_scheme_different_sku_is_a_different_product_and_never_chained():
    a = _norm(sku="A-ELE-0001", name="Nilgiri LED Bulb 9W", category="Electronics", price="100")
    a2 = _norm(sku="A-ELE-0002", name="Nilgiri LED Bulb 9W", category="Electronics", price="140")
    b = _norm(sku="SB/00001-Y", name="LED BULB (Nilgiri) 9W", category="electronics", price="101")
    assert "different_sku" in M.score_pair(a, a2).rules
    result = M.resolve({"a": a, "a2": a2, "b": b})
    cluster_of = {r: i for i, c in enumerate(result.clusters) for r in c}
    assert cluster_of["a"] == cluster_of["b"] != cluster_of["a2"]


def test_resolve_honours_reviewer_decisions():
    a = _norm(sku="X1", name="Konark Steel Bottle 1 l", brand="Konark", price="300")
    b = _norm(sku="X1", name="KONARK STEEL BOTTLE 1L", brand="Konark", price="305")
    assert len(M.resolve({"a": a, "b": b}).clusters) == 1
    assert len(M.resolve({"a": a, "b": b}, rejected=[("a", "b")]).clusters) == 2
    c = _norm(sku="Y9", name="Completely different thing", brand="Other")
    assert len(M.resolve({"a": a, "c": c}, confirmed=[("a", "c")]).clusters) == 1
    with pytest.raises(ValueError):
        M.validate_thresholds(0.5, 0.9)


def test_master_values_and_quality_score():
    members = [
        M.Member("r1", "a", 10, _norm(sku="KON-BOT-1L", name="Konark Steel Bottle 1 l", brand="Konark",
                                      category="Home", price="300")),
        M.Member("r2", "b", 20, _norm(sku="KON-BOT-1L", name="KONARK STEEL BOTTLE 1L", brand="Konark",
                                      category="home", price="310")),
    ]
    values = M.master_values(members)
    assert values["sku"] == "KONBOT1L" and values["name"] == "Konark Steel Bottle 1 l"
    assert values["size"] and values["price"] == 305.0 and values["attributes"].get("material") == "steel"
    q = M.quality_score(values, members)
    assert q["completeness"] == 100.0 and q["consistency"] == 100.0 and 0 < q["overall"] <= 100
    bad = M.quality_score({"name": "AB", "price": -1})
    assert "missing_sku" in bad["issues"] and "invalid_price" in bad["issues"] and bad["overall"] < 50


def test_rule_suggestions_propose_never_apply():
    values = {"name": "COTTON BEDSHEET (Konark) 1 PC", "category": None, "core_name": "cotton bedsheet",
              "attributes": {}, "size": None, "brand": None}
    out = {s.field: s for s in M.rule_suggestions(values, category_by_core={"cotton bedsheet": "Home"})}
    assert out["category"].value == "Home" and out["brand"].value == "Konark"
    assert "attributes.material" in out and out["size"].value
    assert values["category"] is None  # the input is not modified
    upper = {s.field: s for s in M.rule_suggestions({"name": "KONARK COTTON BEDSHEET", "attributes": {}})}
    assert upper["name"].value != "KONARK COTTON BEDSHEET" and not upper["name"].value.isupper()


def test_entity_resolution_on_generated_suppliers():
    """products_supplier_a/b vs products_ground_truth.csv (cross-supplier duplicates, different SKU schemes)."""
    m = product_evaluation()
    assert m["precision"] >= 0.9 and m["recall"] >= 0.85 and m["f1"] >= 0.88, m


def test_entity_resolution_on_generated_catalog():
    """products.csv: duplicate listings merge, pack-size variant families never do."""
    m = catalog_evaluation()
    assert m["precision"] >= 0.95 and m["recall"] >= 0.9 and m["variant_pairs_merged"] == 0, m
