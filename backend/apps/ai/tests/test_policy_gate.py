"""AIDataPolicy gate matrix (AI_ARCHITECTURE.md §5.1 / §5.3) — SECURITY CRITICAL.

Full matrix: 4 policies x 4 classifications x 3 tiers x consent(valid/invalid) x 3 payload classes, against an
independent oracle written directly from the §5.1 table.
"""

from __future__ import annotations

import itertools

import pytest

from apps.ai.policy import check_policy, effective_policy, evaluate_policy, policy_allows
from apps.core.exceptions import AIPolicyBlocked
from nexora.contracts import PayloadClass, PayloadManifest

POLICIES = ["synthetic_only", "schema_only", "aggregates_only", "full"]
CLASSIFICATIONS = ["synthetic", "public", "internal", "confidential"]
TIERS = ["free", "paid", "vertex"]
CLASSES = [PayloadClass.SCHEMA, PayloadClass.VALUES, PayloadClass.DOCUMENT_TEXT]
REF = "dataset:0190a0c4-0000-7000-8000-000000000001"


def oracle(policy: str, classification: str, tier: str, consent: bool, cls: PayloadClass) -> bool:
    """Written from the §5.1 table, independently of the implementation."""
    if classification in ("synthetic", "public"):
        return True  # classified assets: everything allowed under every policy
    # real asset
    if policy == "synthetic_only":
        return False
    if not consent:
        return False  # any policy above synthetic_only needs consent, otherwise synthetic_only applies
    if policy == "schema_only":
        return cls == PayloadClass.SCHEMA
    if tier == "free":
        return False  # aggregates_only / full are refused on the free tier, even if stored in the DB
    if policy == "aggregates_only":
        return cls in (PayloadClass.SCHEMA, PayloadClass.VALUES)
    return True  # full


MATRIX = list(itertools.product(POLICIES, CLASSIFICATIONS, TIERS, [True, False], CLASSES))


@pytest.mark.parametrize(("policy", "classification", "tier", "consent", "cls"), MATRIX,
                         ids=[f"{p}-{c}-{t}-{'consent' if k else 'noconsent'}-{x.value}" for p, c, t, k, x in MATRIX])
def test_policy_matrix(policy, classification, tier, consent, cls):
    payload = PayloadManifest({cls: (REF,)})
    decision = evaluate_policy(payload, state={"policy": policy, "consent_valid": consent},
                               classify=lambda ref: classification, tier=tier)
    assert decision.allowed is oracle(policy, classification, tier, consent, cls)


def test_matrix_size_is_complete():
    assert len(MATRIX) == 4 * 4 * 3 * 2 * 3 == 288


@pytest.mark.parametrize("tier", TIERS)
def test_default_synthetic_only_blocks_real_schema(tier):
    with pytest.raises(AIPolicyBlocked) as exc:
        check_policy(PayloadManifest({PayloadClass.SCHEMA: (REF,)}),
                     state={"policy": "synthetic_only", "consent_valid": True},
                     classify=lambda ref: "internal", tier=tier)
    assert exc.value.get_codes() == "ai_policy_blocked"
    assert exc.value.extra["effective_policy"] == "synthetic_only"
    assert exc.value.extra["blocked"][0]["asset"] == REF


def test_free_tier_refuses_full_policy_stored_in_db():
    decision = evaluate_policy(PayloadManifest({PayloadClass.SCHEMA: (REF,)}),
                               state={"policy": "full", "consent_valid": True},
                               classify=lambda ref: "internal", tier="free")
    assert not decision.allowed
    assert decision.effective_policy == "synthetic_only"
    assert decision.downgrade_reason == "tier_forbidden"
    assert decision.reason == "tier_forbidden"


def test_schema_only_without_consent_is_synthetic_only():
    assert effective_policy("schema_only", False, "free") == ("synthetic_only", "consent_missing")
    assert effective_policy("schema_only", True, "free") == ("schema_only", None)


@pytest.mark.parametrize("garbage", ["", None, "FULL ", "everything", "schema-only"])
def test_unknown_policy_values_fail_closed(garbage):
    eff, reason = effective_policy(garbage, True, "paid")
    if garbage == "FULL ":
        assert eff == "full"  # case/whitespace-insensitive known value
    else:
        assert (eff, reason) == ("synthetic_only", "unknown_policy")


def test_unknown_classification_is_treated_as_confidential():
    decision = evaluate_policy(PayloadManifest({PayloadClass.SCHEMA: (REF,)}),
                               state={"policy": "synthetic_only", "consent_valid": True},
                               classify=lambda ref: "totally-public-trust-me", tier="free")
    assert not decision.allowed
    assert decision.blocked[0].classification == "confidential"


def test_unknown_payload_class_is_refused():
    decision = evaluate_policy(PayloadManifest({"SECRETS": (REF,)}),
                               state={"policy": "full", "consent_valid": True},
                               classify=lambda ref: "synthetic", tier="paid")
    assert not decision.allowed
    assert decision.reason == "unknown_payload_class"


def test_mixed_manifest_blocks_if_any_item_is_forbidden():
    real, synth = "dataset:00000000-0000-7000-8000-00000000000a", "dataset:00000000-0000-7000-8000-00000000000b"
    classes = {real: "internal", synth: "synthetic"}
    payload = PayloadManifest({PayloadClass.SCHEMA: (synth, real), PayloadClass.VALUES: (synth,)})
    ok = evaluate_policy(payload, state={"policy": "schema_only", "consent_valid": True},
                         classify=classes.__getitem__, tier="free")
    assert ok.allowed  # real asset only contributes SCHEMA
    bad = evaluate_policy(PayloadManifest({PayloadClass.VALUES: (synth, real)}),
                          state={"policy": "schema_only", "consent_valid": True},
                          classify=classes.__getitem__, tier="free")
    assert not bad.allowed
    assert [b.asset_ref for b in bad.blocked] == [real]


def test_empty_manifest_is_allowed():
    assert evaluate_policy(PayloadManifest({}), state={"policy": "synthetic_only", "consent_valid": True},
                           classify=lambda r: "internal", tier="free").allowed


def test_classifier_called_once_per_asset():
    calls = []

    def classify(ref):
        calls.append(ref)
        return "public"

    evaluate_policy(PayloadManifest({PayloadClass.SCHEMA: (REF,), PayloadClass.VALUES: (REF,)}),
                    state={"policy": "synthetic_only", "consent_valid": True}, classify=classify, tier="free")
    assert calls == [REF]


def test_check_policy_uses_registry_and_orgs_state_by_default(monkeypatch, settings):
    """Default wiring: apps.orgs.services.ai_policy_state + apps.core.registry.classify_asset + GEMINI_TIER."""
    import apps.core.registry as registry
    import apps.orgs.services as orgs_services

    settings.GEMINI_TIER = "free"
    monkeypatch.setattr(orgs_services, "ai_policy_state", lambda: {"policy": "schema_only", "consent_valid": True})
    monkeypatch.setattr(registry, "_classifiers", {"dataset": lambda uid: "internal"})
    assert policy_allows(PayloadManifest({PayloadClass.SCHEMA: (REF,)}))
    assert not policy_allows(PayloadManifest({PayloadClass.VALUES: (REF,)}))
    # unregistered kinds resolve to confidential (fail closed)
    assert not policy_allows(PayloadManifest({PayloadClass.DOCUMENT_TEXT: ("document:0190a0c4-0000-7000-8000-000000000009",)}))


def test_problem_extra_is_serializable():
    import json

    with pytest.raises(AIPolicyBlocked) as exc:
        check_policy(PayloadManifest({PayloadClass.DOCUMENT_TEXT: (REF,)}),
                     state={"policy": "schema_only", "consent_valid": True}, classify=lambda r: "internal", tier="free")
    json.dumps(exc.value.extra)
    assert exc.value.extra["reason"] == "document_text_not_allowed_under_schema_only"
