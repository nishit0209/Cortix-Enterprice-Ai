"""Enumerations shared by the AI platform (statuses, priorities, tiers)."""

from __future__ import annotations

from enum import StrEnum


class RequestStatus(StrEnum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    INVALID_OUTPUT = "invalid_output"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"


# Statuses for which a request actually reached Google (they count against usage).
NETWORK_STATUSES = frozenset({RequestStatus.OK, RequestStatus.INVALID_OUTPUT, RequestStatus.ERROR,
                              RequestStatus.RATE_LIMITED})


class Priority(StrEnum):
    INTERACTIVE = "interactive"  # chat: may use 100% of the remaining budget
    ASSISTIVE = "assistive"      # narration, descriptions: up to 60%
    BACKGROUND = "background"    # bulk extraction/enrichment: up to 30%


PRIORITY_SHARE: dict[str, float] = {
    Priority.INTERACTIVE: 1.0,
    Priority.ASSISTIVE: 0.6,
    Priority.BACKGROUND: 0.3,
}


class ModelTier(StrEnum):
    FAST = "fast"
    REASONING = "reasoning"
    VISION = "vision"


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN_MINUTE = "open_minute"
    OPEN_DAY = "open_day"


GEMINI_TIERS = ("free", "paid", "vertex")
PACIFIC_TZ = "America/Los_Angeles"

# Models that must never be configured (AI_ARCHITECTURE.md §2.2).
DEPRECATED_MODELS = frozenset({
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.0-pro",
    "gemini-3-pro-preview", "gemini-3.1-flash-lite-preview", "gemini-1.5-pro", "gemini-1.5-flash",
})

# Error codes (problem+json `code`) produced by the AI platform.
CODE_NOT_CONFIGURED = "ai_not_configured"
CODE_POLICY_BLOCKED = "ai_policy_blocked"
CODE_QUOTA_EXHAUSTED = "ai_quota_exhausted"
CODE_RATE_LIMITED = "ai_rate_limited"
CODE_UNAVAILABLE = "ai_unavailable"
CODE_INVALID_OUTPUT = "ai_invalid_output"
CODE_LIMITER_UNAVAILABLE = "ai_limiter_unavailable"
