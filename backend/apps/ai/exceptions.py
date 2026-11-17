"""AI-specific errors. The quota / availability / policy errors live in apps.core.exceptions."""

from __future__ import annotations

from apps.core.exceptions import AIPolicyBlocked, AIQuotaExhausted, AIUnavailable, NexoraError

from .constants import CODE_INVALID_OUTPUT


class AIInvalidOutput(NexoraError):
    """Gemini returned output that failed schema validation even after one repair (or was safety-blocked)."""

    status_code = 502
    default_code = CODE_INVALID_OUTPUT
    default_detail = "The AI could not produce a valid answer. You can edit the query manually."


__all__ = ["AIInvalidOutput", "AIPolicyBlocked", "AIQuotaExhausted", "AIUnavailable"]
