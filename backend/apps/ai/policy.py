"""AIDataPolicy gate (SECURITY CRITICAL) — AI_ARCHITECTURE.md §5.1 / §5.3.

`check_policy(payload)` runs inside GeminiService BEFORE any network call. It loads the org's policy and
consent validity (`apps.orgs.services.ai_policy_state()`), each asset's classification
(`apps.core.registry.classify_asset`) and the server-side `settings.GEMINI_TIER`, and either returns an
allowing `PolicyDecision` or raises `AIPolicyBlocked`.

Rules (fail closed everywhere):
* Unknown/garbled policy values, a policy above `synthetic_only` without valid consent, and
  `aggregates_only` / `full` on the FREE tier (even if stored in the DB) all collapse to `synthetic_only`.
* Classified assets (`synthetic` / `public`) may always be sent.
* Real assets (`internal` / `confidential` / unknown):
    synthetic_only  -> nothing at all
    schema_only     -> SCHEMA only
    aggregates_only -> SCHEMA + VALUES (aggregates; caller-declared)   [paid / vertex only]
    full            -> SCHEMA + VALUES + DOCUMENT_TEXT                  [paid / vertex only]
* Unknown payload classes are refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from django.conf import settings

from apps.core.exceptions import AIPolicyBlocked
from nexora.contracts import AIDataPolicy, AssetClassification, PayloadClass, PayloadManifest

from .constants import CODE_POLICY_BLOCKED

CLASSIFIED = frozenset({AssetClassification.SYNTHETIC.value, AssetClassification.PUBLIC.value})
_KNOWN_CLASSIFICATIONS = frozenset(c.value for c in AssetClassification)
PAID_TIERS = frozenset({"paid", "vertex"})

ALLOWED_FOR_REAL: dict[str, frozenset[str]] = {
    AIDataPolicy.SYNTHETIC_ONLY.value: frozenset(),
    AIDataPolicy.SCHEMA_ONLY.value: frozenset({PayloadClass.SCHEMA.value}),
    AIDataPolicy.AGGREGATES_ONLY.value: frozenset({PayloadClass.SCHEMA.value, PayloadClass.VALUES.value}),
    AIDataPolicy.FULL.value: frozenset(
        {PayloadClass.SCHEMA.value, PayloadClass.VALUES.value, PayloadClass.DOCUMENT_TEXT.value}
    ),
}


@dataclass(frozen=True)
class BlockedItem:
    payload_class: str
    asset_ref: str
    classification: str
    reason: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    policy: str                 # as stored on the org
    effective_policy: str       # after consent / tier enforcement
    consent_valid: bool
    tier: str
    downgrade_reason: str | None = None  # "consent_missing" | "tier_forbidden" | "unknown_policy"
    blocked: tuple[BlockedItem, ...] = field(default_factory=tuple)
    payload_classes: tuple[str, ...] = ()
    asset_refs: tuple[str, ...] = ()

    @property
    def reason(self) -> str | None:
        if self.allowed:
            return None
        return self.blocked[0].reason if self.blocked else "blocked"

    def as_problem_extra(self) -> dict:
        return {
            "policy": self.policy,
            "effective_policy": self.effective_policy,
            "gemini_tier": self.tier,
            "reason": self.reason,
            "blocked": [
                {"payload_class": b.payload_class, "asset": b.asset_ref, "classification": b.classification,
                 "reason": b.reason}
                for b in self.blocked
            ],
        }


def effective_policy(policy: str | None, consent_valid: bool, tier: str) -> tuple[str, str | None]:
    """Collapse the stored policy to what may actually be applied. Returns (effective, downgrade_reason)."""
    synthetic = AIDataPolicy.SYNTHETIC_ONLY.value
    value = (policy or "").strip().lower()
    if value not in ALLOWED_FOR_REAL:
        return synthetic, "unknown_policy"
    if value == synthetic:
        return synthetic, None
    if not consent_valid:
        return synthetic, "consent_missing"
    if value in (AIDataPolicy.AGGREGATES_ONLY.value, AIDataPolicy.FULL.value) and tier not in PAID_TIERS:
        return synthetic, "tier_forbidden"
    return value, None


def _normalize_classification(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if value in _KNOWN_CLASSIFICATIONS else AssetClassification.CONFIDENTIAL.value


def _normalize_items(payload: PayloadManifest) -> list[tuple[str, tuple[str, ...]]]:
    items: Mapping = payload.items if payload is not None else {}
    out: list[tuple[str, tuple[str, ...]]] = []
    for key, refs in items.items():
        cls = key.value if isinstance(key, PayloadClass) else str(key)
        if isinstance(refs, str):
            refs = (refs,)
        out.append((cls, tuple(str(r) for r in (refs or ()))))
    return out


def evaluate_policy(
    payload: PayloadManifest,
    *,
    state: Mapping,
    classify: Callable[[str], str],
    tier: str,
) -> PolicyDecision:
    """Pure decision function (no I/O besides `classify`)."""
    tier = (tier or "free").strip().lower()
    stored = str(state.get("policy") or "")
    consent_valid = bool(state.get("consent_valid"))
    eff, downgrade = effective_policy(stored, consent_valid, tier)
    allowed_real = ALLOWED_FOR_REAL[eff]
    known_classes = {c.value for c in PayloadClass}

    blocked: list[BlockedItem] = []
    classes: list[str] = []
    refs_all: list[str] = []
    cache: dict[str, str] = {}
    for cls, refs in _normalize_items(payload):
        classes.append(cls)
        if cls not in known_classes:
            blocked.append(BlockedItem(cls, ",".join(refs), "-", "unknown_payload_class"))
            continue
        for ref in refs:
            refs_all.append(ref)
            if ref not in cache:
                cache[ref] = _normalize_classification(classify(ref))
            classification = cache[ref]
            if classification in CLASSIFIED:
                continue
            if cls in allowed_real:
                continue
            if eff == AIDataPolicy.SYNTHETIC_ONLY.value:
                reason = downgrade or "real_asset_under_synthetic_only"
            else:
                reason = f"{cls.lower()}_not_allowed_under_{eff}"
            blocked.append(BlockedItem(cls, ref, classification, reason))
    return PolicyDecision(
        allowed=not blocked,
        policy=stored or AIDataPolicy.SYNTHETIC_ONLY.value,
        effective_policy=eff,
        consent_valid=consent_valid,
        tier=tier,
        downgrade_reason=downgrade,
        blocked=tuple(blocked),
        payload_classes=tuple(dict.fromkeys(classes)),
        asset_refs=tuple(dict.fromkeys(refs_all)),
    )


def _load_state() -> dict:
    from apps.orgs.services import ai_policy_state

    return ai_policy_state()


def _classifier() -> Callable[[str], str]:
    from apps.core.registry import classify_asset

    return classify_asset


def decide(payload: PayloadManifest, *, state: Mapping | None = None,
           classify: Callable[[str], str] | None = None, tier: str | None = None) -> PolicyDecision:
    return evaluate_policy(
        payload,
        state=state if state is not None else _load_state(),
        classify=classify or _classifier(),
        tier=tier if tier is not None else str(getattr(settings, "GEMINI_TIER", "free")),
    )


def check_policy(payload: PayloadManifest, *, state: Mapping | None = None,
                 classify: Callable[[str], str] | None = None, tier: str | None = None) -> PolicyDecision:
    """AIPolicyGate.check — returns the decision or raises AIPolicyBlocked (no network involved)."""
    decision = decide(payload, state=state, classify=classify, tier=tier)
    if not decision.allowed:
        raise AIPolicyBlocked(_message(decision), code=CODE_POLICY_BLOCKED, extra=decision.as_problem_extra())
    return decision


def policy_allows(payload: PayloadManifest, **kwargs) -> bool:
    """Non-raising convenience (e.g. to choose Gemini narration vs. a local template)."""
    return decide(payload, **kwargs).allowed


def _message(decision: PolicyDecision) -> str:
    if decision.downgrade_reason == "tier_forbidden":
        return (f"The '{decision.policy}' AI data policy is not permitted on the Gemini {decision.tier} tier, so "
                "the organization is treated as 'synthetic_only'. Only synthetic or public assets can be sent.")
    if decision.downgrade_reason == "consent_missing":
        return ("The organization's AI data policy has no valid consent, so it is treated as 'synthetic_only'. "
                "An owner or admin must (re-)consent before real data can be described to Gemini.")
    if decision.effective_policy == AIDataPolicy.SYNTHETIC_ONLY.value:
        return ("Your organization's AI data policy is 'synthetic_only': only datasets and documents classified "
                "synthetic or public may be sent to Gemini. An admin can change the policy.")
    return (f"Your organization's AI data policy '{decision.effective_policy}' does not allow sending this kind "
            "of data to Gemini.")
