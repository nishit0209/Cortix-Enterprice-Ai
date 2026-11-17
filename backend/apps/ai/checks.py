"""Django system checks for the AI configuration (offline: no network call, no quota spent).

The live `models.list` verification runs in scripts/verify_gemini.py (AI_ARCHITECTURE.md §2.5).
"""

from __future__ import annotations

from django.conf import settings
from django.core import checks

from .constants import DEPRECATED_MODELS, GEMINI_TIERS


@checks.register("nexora_ai")
def validate_ai_settings(app_configs=None, **kwargs):
    problems = []
    level = checks.Warning if settings.DEBUG else checks.Error
    tier = str(getattr(settings, "GEMINI_TIER", "free")).lower()
    if tier not in GEMINI_TIERS:
        problems.append(level(f"GEMINI_TIER={tier!r} is invalid (free|paid|vertex).", id="nexora_ai.E001"))
    for name in ("GEMINI_MODEL_FAST", "GEMINI_MODEL_REASONING", "GEMINI_MODEL_VISION"):
        model = getattr(settings, name, "") or ""
        if model and model in DEPRECATED_MODELS:
            problems.append(level(f"{name}={model} is deprecated or shut down; configure a supported model.",
                                  id="nexora_ai.E002"))
    if tier == "vertex" and not (getattr(settings, "GEMINI_VERTEX_PROJECT", "")
                                 and getattr(settings, "GEMINI_VERTEX_LOCATION", "")):
        problems.append(level("GEMINI_TIER=vertex requires GEMINI_VERTEX_PROJECT and GEMINI_VERTEX_LOCATION.",
                              id="nexora_ai.E003"))
    if str(getattr(settings, "EMBEDDING_PROVIDER", "local")).lower() != "local":
        problems.append(checks.Warning("Only EMBEDDING_PROVIDER=local is implemented.", id="nexora_ai.W001"))
    return problems
