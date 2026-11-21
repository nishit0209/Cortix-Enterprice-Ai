"""Customer 360 pure functions (no DB): normalization, blocking, scoring, union-find clustering, survivorship, RFM,
rule segments and the entity-resolution evaluation on testdata/generated (CRM + web vs ground truth)."""

import datetime as dt

import pytest

from apps.customers import matching as M
from apps.products.tests.er_eval import customer_evaluation
from apps.quality.services import UnionFind, candidate_pairs, reconcile_clusters, soundex


def test_email_phone_name_date_normalization():
    assert M.normalize_email("  Pooja.Kapoor+news@Example.ORG ") == ("pooja.kapoor+news@example.org",
                                                                      "poojakapoor@example.org")
    assert M.normalize_email("not-an-email") == (None, None)
    assert M.normalize_phone("+91-94891 23942") == "+919489123942"
    assert M.normalize_phone("09489123942") == "+919489123942"
    assert M.normalize_phone("9489123942", "1") == "+19489123942"
    assert M.normalize_phone("123") is None
    assert M.normalize_name("Kapoor, Pooja") == "pooja kapoor"
    assert M.normalize_name(first="Zoë", last="Ãlvarez") == "zoe alvarez"
    assert M.normalize_date("09/12/1970") == "1970-12-09" and M.normalize_date("1970-12-09T00:00:00") == "1970-12-09"
    assert M.parse_timestamp("2024-01-02").tzinfo is not None


def test_blocking_keys_and_candidate_pairs():
    a = M.normalize_record({"name": "Pooja Kapoor", "email": "pooja@example.org", "phone": "9230026611"})
    b = M.normalize_record({"name": "Puja Kapur", "email": "pk@example.org", "phone": "+91 92300 26611"})
    c = M.normalize_record({"name": "Rahul Mehta", "email": "rahul@other.com", "phone": "9000000001"})
    keys = {k: M.blocking_keys(n) for k, n in {"a": a, "b": b, "c": c}.items()}
    pairs, stats = candidate_pairs(keys)
    assert ("a", "b") in pairs and not any("c" in p for p in pairs)
    assert stats.as_dict()["pairs"] == len(pairs)
    assert soundex("Kapoor") == soundex("Kapur")


def test_pair_scoring_and_decisions():
    a = M.normalize_record({"name": "Pooja Kapoor", "email": "pooja.kapoor576@example.org", "phone": "9230026611",
                            "dob": "1970-12-09", "address": "Hyderabad"})
    b = M.normalize_record({"name": "Pooja Kapoor", "email": "POOJA.KAPOOR576@example.org", "phone": "+91-9230026611",
                            "address": "hyderabad"})
    s = M.score_pair(a, b)
    assert s.score >= M.DEFAULT_AUTO and M.decide(s.score) == "auto_merged"
    c = M.normalize_record({"name": "Pooja Kapoor", "email": "someone.else@example.org", "address": "Pune"})
    assert M.decide(M.score_pair(a, c).score) in (None, "pending_review")
    with pytest.raises(ValueError):
        M.validate_thresholds(0.6, 0.8)


def test_union_find_respects_cannot_links_and_reconciles_ids():
    uf = UnionFind(["a", "b", "c"])
    uf.forbid("a", "c")
    assert uf.union("a", "b") is True and uf.union("b", "c") is False
    groups = sorted(sorted(g) for g in uf.groups())
    assert groups == [["a", "b"], ["c"]]
    assignments = reconcile_clusters([["a", "b"], ["c"]], {"a": "P1", "b": "P1", "c": "P1"})
    owners = {tuple(sorted(c)): pid for pid, c in assignments}
    assert owners[("a", "b")] == "P1" and owners[("c",)] is None


def test_resolve_clusters_with_decisions():
    recs = {
        "r1": M.normalize_record({"name": "Asha Rao", "email": "asha@example.org", "phone": "9000000001"}),
        "r2": M.normalize_record({"name": "Asha Rao", "email": "asha@example.org", "phone": "9000000001"}),
        "r3": M.normalize_record({"name": "Vikram Shah", "email": "vik@example.org"}),
    }
    assert len(M.resolve(recs).clusters) == 2
    assert len(M.resolve(recs, rejected=[("r1", "r2")]).clusters) == 3
    assert len(M.resolve(recs, confirmed=[("r1", "r3")]).clusters) == 1


def test_survivorship_rules():
    cands = [
        M.Candidate("r1", "crm", 10, "2023-01-01T00:00:00+00:00", {"name": "Asha Rao", "email": "old@example.org",
                                                                   "phone": "+919000000001", "external_id": "C1"}),
        M.Candidate("r2", "web", 20, "2024-06-01T00:00:00+00:00", {"name": "Asha R.", "email": "new@example.org",
                                                                   "phone": "+919000000001", "external_id": "W1"}),
    ]
    golden, trail = M.golden_record(cands, {"email": "most_recent", "name": "source_priority",
                                            "phone": "most_frequent"})
    assert golden["email"] == "new@example.org" and trail["email"]["source"] == "web"
    assert golden["name"] == "Asha Rao" and trail["name"]["rule"] == "source_priority"
    assert golden["phone"] == "+919000000001" and golden["source_ids"] == {"crm": ["C1"], "web": ["W1"]}
    longest, _ = M.golden_record(cands, {"name": "longest"})
    assert longest["name"] == "Asha Rao"


def test_rfm_quintiles_and_labels():
    as_of = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    stats = [(f"p{i}", as_of - dt.timedelta(days=i * 30), 10 - i, 1000.0 - i * 100) for i in range(10)]
    rows = {r.profile_id: r for r in M.compute_rfm(stats, as_of)}
    assert rows["p0"].r == 5 and rows["p0"].f == 5 and rows["p0"].label == "Champions"
    assert rows["p9"].r == 1 and rows["p9"].label == "Hibernating"
    assert M.quintile_scores([1, 1, 1, 1]) == [1, 1, 1, 1]
    assert M.compute_rfm([("x", None, 0, 0.0)], as_of) == []


def test_rule_segment_conditions():
    definition = M.validate_conditions({"match": "all", "conditions": [
        {"field": "monetary", "op": ">=", "value": 500}, {"field": "golden.email", "op": "not_null"}]})
    assert definition["conditions"][0]["op"] == "gte"
    assert M.matches_definition({"monetary": 700, "golden": {"email": "a@b.co"}}, definition)
    assert not M.matches_definition({"monetary": 100, "golden": {"email": "a@b.co"}}, definition)
    with pytest.raises(ValueError):
        M.validate_conditions({"conditions": [{"field": "password", "op": "eq", "value": 1}]})


def test_entity_resolution_on_generated_crm_and_web():
    """customers_crm.csv + customers_web.csv vs customers_ground_truth.csv."""
    m = customer_evaluation()
    assert m["precision"] >= 0.95 and m["recall"] >= 0.95 and m["f1"] >= 0.95, m
