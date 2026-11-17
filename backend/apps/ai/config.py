"""Runtime configuration: settings-derived model tiers + DB-backed org budgets and model quotas."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError

from .constants import ModelTier
from .quota import ModelLimits, OrgLimits, default_model_limits

log = logging.getLogger("nexora.ai")


@dataclass(frozen=True)
class OrgAIConfig:
    daily_request_budget: int = 50
    daily_token_budget: int = 250_000
    per_user_daily_requests: int = 20
    log_prompt_text: bool = False

    def limits(self) -> OrgLimits:
        return OrgLimits(self.daily_request_budget, self.daily_token_budget, self.per_user_daily_requests)


def gemini_tier() -> str:
    return str(getattr(settings, "GEMINI_TIER", "free") or "free").strip().lower()


def is_configured() -> bool:
    """AI is configured when credentials for the selected tier exist. Never falls back to anything else."""
    if gemini_tier() == "vertex":
        return bool(getattr(settings, "GEMINI_VERTEX_PROJECT", "")) and bool(
            getattr(settings, "GEMINI_VERTEX_LOCATION", ""))
    return bool(getattr(settings, "GEMINI_API_KEY", ""))


def model_for_tier(tier: str) -> str:
    tier = (tier or ModelTier.FAST).lower()
    if tier == ModelTier.REASONING:
        return settings.GEMINI_MODEL_REASONING
    if tier == ModelTier.VISION:
        return getattr(settings, "GEMINI_MODEL_VISION", "") or settings.GEMINI_MODEL_FAST
    if tier == ModelTier.FAST:
        return settings.GEMINI_MODEL_FAST
    raise ValueError(f"Unknown model tier {tier!r}")


def request_timeout_s() -> float:
    return float(getattr(settings, "GEMINI_REQUEST_TIMEOUT_S", 60) or 60)


def max_output_tokens() -> int:
    return int(getattr(settings, "GEMINI_MAX_OUTPUT_TOKENS", 2048) or 2048)


def load_org_config(org_id: UUID | None = None) -> OrgAIConfig:
    """Read AIOrgConfig for the current tenant (defaults when absent). Must run in a tenant context."""
    from .models import AIOrgConfig

    try:
        row = AIOrgConfig.objects.first()
    except DatabaseError:
        log.exception("ai: could not load AIOrgConfig, using defaults")
        return OrgAIConfig()
    if row is None:
        return OrgAIConfig()
    return OrgAIConfig(
        daily_request_budget=row.daily_request_budget,
        daily_token_budget=row.daily_token_budget,
        per_user_daily_requests=row.per_user_daily_requests,
        log_prompt_text=row.log_prompt_text,
    )


def load_model_limits(model: str) -> ModelLimits:
    from .models import AIModelQuota

    try:
        row = AIModelQuota.objects.filter(model=model).first()
    except DatabaseError:
        log.exception("ai: could not load AIModelQuota for %s, using defaults", model)
        row = None
    if row is None:
        return default_model_limits(model)
    return ModelLimits(rpm=row.rpm, tpm=row.tpm, rpd=row.rpd)
