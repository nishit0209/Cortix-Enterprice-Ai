"""Public service API of apps.ai. Other apps import only from here.

    from apps.ai.services import gemini, check_policy
    data = gemini().generate_structured(feature="nl2sql", prompt_key="nl2sql.generate", system=..., prompt=...,
                                        schema=..., payload=PayloadManifest({PayloadClass.SCHEMA: ("dataset:<id>",)}))
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Count, Sum

from apps.core import audit
from apps.core.tenancy.context import require_org_id

from . import config as cfgmod
from .constants import BreakerState, ModelTier
from .embeddings import (
    EmbeddingUnavailable,
    FakeEmbedder,
    LocalEmbedder,
    UnavailableEmbedder,
    embed_query,
    embedder_status,
    embedding_available,
    get_embedder,
    override_embedder,
)
from .engine import GeminiEngine, estimate_tokens, request_trace
from .policy import PolicyDecision, check_policy, decide, effective_policy, policy_allows
from .prompts import data_block, get_prompt
from .quota import LimiterUnavailable
from .redaction import redact
from .timeutil import next_pacific_midnight, pacific_day

__all__ = [
    "EmbeddingUnavailable", "FakeEmbedder", "LocalEmbedder", "PolicyDecision", "UnavailableEmbedder", "ai_status",
    "check_policy", "data_block", "decide", "embed_query", "embedder_status", "embedding_available",
    "estimate_tokens", "gemini", "get_embedder", "get_org_config", "get_prompt", "override_embedder",
    "override_gemini", "policy_allows", "redact", "request_trace", "set_gemini", "standalone_engine",
    "update_org_config", "usage_summary",
]

_engine: GeminiEngine | None = None
_engine_lock = threading.Lock()


def gemini() -> GeminiEngine:
    """The process-wide GeminiService singleton (contract: nexora.contracts.GeminiService)."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = GeminiEngine()
    return _engine


def set_gemini(engine: GeminiEngine | None) -> None:
    global _engine
    _engine = engine


def standalone_engine(*, transport_factory=None, classification: str = "synthetic", sleep=None) -> GeminiEngine:
    """A GeminiEngine for developer tools that run WITHOUT a database (`manage.py record_cassettes`).

    * the REAL policy gate, evaluated for an org on the default `synthetic_only` policy with every referenced
      asset classified `classification` (the tool's own synthetic fixtures), on the configured GEMINI_TIER;
    * quota counters in process memory with the model's DEFAULT project limits (RPM/TPM/RPD pacing), generous
      org budgets (the tool is not an org);
    * request records kept in memory (`engine._recorder.records`) and written to the structured log;
    * the real Gemini transport unless `transport_factory` is given (tests pass a fake one).
    The caller sets the tenant ContextVar (apps.core.tenancy.context.current_org) for the call.
    """
    import time as _time

    from .quota import QuotaLimiter, default_model_limits
    from .quota_store import DbRedis, MemoryCounterBackend
    from .recorder import InMemoryRecorder

    store = DbRedis(MemoryCounterBackend())

    def _policy(payload):
        return check_policy(payload, state={"policy": "synthetic_only", "consent_valid": False},
                            classify=lambda ref: classification, tier=cfgmod.gemini_tier())

    return GeminiEngine(
        transport_factory=transport_factory,
        limiter=QuotaLimiter(lambda: store),
        recorder=InMemoryRecorder(),
        org_config_loader=lambda org_id: cfgmod.OrgAIConfig(daily_request_budget=100_000,
                                                            daily_token_budget=100_000_000,
                                                            per_user_daily_requests=100_000),
        model_limits_loader=default_model_limits,
        policy_check=_policy,
        sleep=sleep or _time.sleep,
    )


@contextmanager
def override_gemini(engine: GeminiEngine) -> Iterator[GeminiEngine]:
    """Swap the singleton (tests inject an engine built with apps.ai.testing.build_engine)."""
    global _engine
    previous = _engine
    _engine = engine
    try:
        yield engine
    finally:
        _engine = previous


# --------------------------------------------------------------------------------------- org config
def get_org_config() -> cfgmod.OrgAIConfig:
    return cfgmod.load_org_config(require_org_id())


ORG_CONFIG_FIELDS = ("daily_request_budget", "daily_token_budget", "per_user_daily_requests", "log_prompt_text")


def update_org_config(*, request=None, **changes) -> cfgmod.OrgAIConfig:
    """Create/update AIOrgConfig for the current tenant (caller checks `org.settings.edit`). Audited."""
    from .models import AIOrgConfig

    unknown = set(changes) - set(ORG_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"Unknown AI config fields: {sorted(unknown)}")
    for key in ("daily_request_budget", "daily_token_budget", "per_user_daily_requests"):
        if key in changes and (not isinstance(changes[key], int) or changes[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    row = AIOrgConfig.objects.first() or AIOrgConfig()
    before = {f: getattr(row, f) for f in ORG_CONFIG_FIELDS}
    for k, v in changes.items():
        setattr(row, k, v)
    row.save()
    after = {f: getattr(row, f) for f in ORG_CONFIG_FIELDS}
    audit.record("ai.config.update", target=row,
                 changes={k: [before[k], after[k]] for k in ORG_CONFIG_FIELDS if before[k] != after[k]},
                 request=request)
    return cfgmod.load_org_config(require_org_id())


# --------------------------------------------------------------------------------------- status / usage
def _policy_state() -> dict:
    from apps.orgs.services import ai_policy_state

    return ai_policy_state()


def _db_usage_today() -> tuple[int, int]:
    from .models import AIUsageDaily

    try:
        agg = AIUsageDaily.objects.filter(day=pacific_day()).aggregate(
            req=Sum("requests"), inp=Sum("input_tokens"), out=Sum("output_tokens"))
    except DatabaseError:
        return 0, 0
    return int(agg["req"] or 0), int((agg["inp"] or 0) + (agg["out"] or 0))


LOW_QUOTA_SHARE = 0.2  # "low" when at most 20% of a daily budget is left


def quota_state(*, used: int, limit: int, project_used: int | None = None, project_limit: int | None = None,
                breaker: str = "closed", breaker_resets_at: str | None = None, budget_resets_at: str,
                tokens_used: int = 0, tokens_limit: int | None = None, user_used: int | None = None,
                user_limit: int | None = None) -> dict:
    """The `quota` block of GET /ai/status (CONTRACT W3 "AI quota UX"). Pure.

    `state` is "exhausted" when the next interactive request would be refused for the day: the day breaker is open,
    the platform's daily project quota for the chat model is used, the org's daily request or token budget is used
    (or is 0), or the viewer's personal daily cap is used. "low" when at most 20% of one of those budgets is left;
    otherwise "ok". `scope` names the budget that decided it (platform | org_requests | org_tokens | user | null).
    `used_today` / `limit_today` are the org's request budget.
    """
    used, limit = max(0, int(used)), max(0, int(limit))

    def _out(u: int | None, lim: int | None) -> bool:
        return lim is not None and int(lim) >= 0 and (int(lim) == 0 or int(u or 0) >= int(lim))

    def _low(u: int | None, lim: int | None) -> bool:
        return bool(lim) and (int(lim) - int(u or 0)) <= max(1, math.ceil(int(lim) * LOW_QUOTA_SHARE))

    budgets = (  # (scope, used, limit) in the order a refusal is reported
        ("platform", project_used, project_limit or None),
        ("org_requests", used, limit),
        ("org_tokens", tokens_used, tokens_limit),
        ("user", user_used, user_limit if user_used is not None else None),
    )
    scope = "platform" if breaker == BreakerState.OPEN_DAY.value else None
    if scope is None:
        scope = next((name for name, u, lim in budgets if _out(u, lim)), None)
    state = "exhausted" if scope else "ok"
    if scope is None:
        scope = next((name for name, u, lim in budgets if _low(u, lim)), None)
        state = "low" if scope else "ok"
    resets_at = breaker_resets_at if breaker == BreakerState.OPEN_DAY.value and breaker_resets_at else budget_resets_at
    return {"state": state, "resets_at": resets_at, "used_today": used, "limit_today": limit,
            "remaining_today": max(0, limit - used), "scope": scope,
            "rate_limited": breaker == BreakerState.OPEN_MINUTE.value}


def ai_status(user_id: UUID | None = None) -> dict:
    """GET /orgs/{org}/ai/status payload. Inside a tenant context."""
    org_id = require_org_id()
    state = _policy_state()
    tier = cfgmod.gemini_tier()
    eff, downgrade = effective_policy(state.get("policy"), bool(state.get("consent_valid")), tier)
    org_cfg = cfgmod.load_org_config(org_id)
    engine = gemini()
    models: dict[str, dict] = {}
    limiter_ok = True
    used_req = used_tok = 0
    user_used: int | None = None
    project_used: int | None = None
    try:
        snap = engine.limiter.usage(org_id, user_id)
        used_req, used_tok, user_used = snap.org_requests, snap.org_tokens, snap.user_requests
        project_used = engine.limiter.project_day_requests(cfgmod.model_for_tier(ModelTier.FAST))
        for tier_name in (ModelTier.FAST, ModelTier.REASONING):
            model = cfgmod.model_for_tier(tier_name)
            br = engine.limiter.breaker(model)
            models[tier_name.value] = {"model": model, "breaker": br.state,
                                       "resets_at": br.resets_at.isoformat() if br.resets_at else None}
    except LimiterUnavailable:
        limiter_ok = False
        used_req, used_tok = _db_usage_today()
        for tier_name in (ModelTier.FAST, ModelTier.REASONING):
            models[tier_name.value] = {"model": cfgmod.model_for_tier(tier_name), "breaker": "unknown",
                                       "resets_at": None}

    primary = models[ModelTier.FAST.value]
    breaker = primary["breaker"] if primary["breaker"] in {s.value for s in BreakerState} else "closed"
    budget_reset = next_pacific_midnight().isoformat()
    resets_at = primary["resets_at"] if breaker != BreakerState.CLOSED.value else budget_reset
    project_limit = None
    if project_used is not None:
        project_limit = cfgmod.load_model_limits(cfgmod.model_for_tier(ModelTier.FAST)).effective().rpd
    quota = quota_state(used=used_req, limit=org_cfg.daily_request_budget, project_used=project_used,
                        project_limit=project_limit, breaker=breaker, breaker_resets_at=primary["resets_at"],
                        budget_resets_at=budget_reset, tokens_used=used_tok, tokens_limit=org_cfg.daily_token_budget,
                        user_used=user_used, user_limit=org_cfg.per_user_daily_requests)
    if user_used is not None:
        quota["user"] = {"used": user_used, "limit": org_cfg.per_user_daily_requests}
    return {
        "policy": eff,
        "stored_policy": state.get("policy"),
        "consent_valid": bool(state.get("consent_valid")),
        "policy_downgrade_reason": downgrade,
        "tier": tier,
        "configured": cfgmod.is_configured(),
        "org_budget": {"limit": org_cfg.daily_request_budget, "used": used_req},
        "token_budget": {"limit": org_cfg.daily_token_budget, "used": used_tok},
        "user_budget": ({"limit": org_cfg.per_user_daily_requests, "used": user_used}
                        if user_used is not None else None),
        "breaker": breaker,
        "resets_at": resets_at,
        "budget_resets_at": budget_reset,
        "models": models,
        "limiter_available": limiter_ok,
        "limiter_backend": "redis" if str(getattr(settings, "REDIS_URL", "") or "").strip() else "db",
        "quota": quota,
        "embeddings": embedder_status_safe(),
    }


def embedder_status_safe() -> dict:
    """Local embedding model status for the UI (never loads or downloads the model)."""
    st = embedder_status()
    return {"available": bool(st.get("available")), "model": st.get("model"), "cached": st.get("cached"),
            "loaded": st.get("loaded")}


def usage_summary(days: int = 7) -> dict:
    """GET /orgs/{org}/ai/usage payload: per-day/model counters + per-feature/status breakdown."""
    from .models import AIRequestLog, AIUsageDaily

    require_org_id()
    days = max(1, min(int(days), 90))
    end = pacific_day()
    start = end - timedelta(days=days - 1)
    daily = [
        {"day": r.day.isoformat(), "model": r.model, "requests": r.requests, "input_tokens": r.input_tokens,
         "output_tokens": r.output_tokens, "cached_tokens": r.cached_tokens, "blocked": r.blocked,
         "errors": r.errors}
        for r in AIUsageDaily.objects.filter(day__gte=start, day__lte=end).order_by("day", "model")
    ]
    since = next_pacific_midnight(end) - timedelta(days=days)
    logs = AIRequestLog.objects.filter(created_at__gte=since)
    by_feature = [
        {"feature": r["feature"], "requests": r["n"], "input_tokens": r["inp"] or 0, "output_tokens": r["out"] or 0}
        for r in logs.values("feature").annotate(n=Count("id"), inp=Sum("input_tokens"), out=Sum("output_tokens"))
        .order_by("-n", "feature")
    ]
    by_status = {r["status"]: r["n"] for r in logs.values("status").annotate(n=Count("id")).order_by("status")}
    totals = {
        "requests": sum(d["requests"] for d in daily),
        "input_tokens": sum(d["input_tokens"] for d in daily),
        "output_tokens": sum(d["output_tokens"] for d in daily),
        "cached_tokens": sum(d["cached_tokens"] for d in daily),
        "blocked": sum(d["blocked"] for d in daily),
        "errors": sum(d["errors"] for d in daily),
    }
    return {
        "range": {"from": start.isoformat(), "to": end.isoformat(), "days": days, "timezone": "America/Los_Angeles"},
        "totals": totals,
        "daily": daily,
        "by_feature": by_feature,
        "by_status": by_status,
    }
