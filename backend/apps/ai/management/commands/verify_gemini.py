"""python manage.py verify_gemini — check that the configured Gemini model ids exist for these credentials.

Lists the models visible to the configured credentials (a metadata call: NO generation request, no prompt, no
data is sent) and checks GEMINI_MODEL_FAST / GEMINI_MODEL_REASONING / GEMINI_MODEL_VISION against it.

Exit codes: 0 all configured models exist (or nothing to verify: no credentials and no --require-key),
            1 at least one configured model is missing or cannot generate content,
            2 the catalogue could not be listed (auth / network error), or --require-key without credentials.
Tests never run this against the network (the listing function is injectable).
"""

from __future__ import annotations

import json
from collections.abc import Callable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ai import config as cfgmod
from apps.ai.constants import ModelTier

GENERATE_ACTION = "generateContent"


def configured_models() -> dict[str, str]:
    """{setting name: model id} for every model tier that is configured (vision falls back to fast)."""
    out = {
        "GEMINI_MODEL_FAST": cfgmod.model_for_tier(ModelTier.FAST),
        "GEMINI_MODEL_REASONING": cfgmod.model_for_tier(ModelTier.REASONING),
    }
    if getattr(settings, "GEMINI_MODEL_VISION", ""):
        out["GEMINI_MODEL_VISION"] = cfgmod.model_for_tier(ModelTier.VISION)
    return out


def _default_lister() -> list[dict]:
    from apps.ai.gemini import GenaiTransport

    return GenaiTransport().list_models()


def verify(lister: Callable[[], list[dict]]) -> dict:
    """Pure check: returns a report dict {ok, tier, models: [{setting, model, found, can_generate, ...}]}."""
    catalogue = {m["id"]: m for m in lister()}
    rows = []
    for setting, model in configured_models().items():
        info = catalogue.get(model)
        actions = list((info or {}).get("actions") or [])
        # Vertex listings do not always carry supported actions: absence of the list is not a failure.
        can_generate = bool(info) and (not actions or GENERATE_ACTION in actions)
        rows.append({
            "setting": setting, "model": model, "found": info is not None, "can_generate": can_generate,
            "input_token_limit": (info or {}).get("input_token_limit"),
            "output_token_limit": (info or {}).get("output_token_limit"),
        })
    return {"ok": all(r["found"] and r["can_generate"] for r in rows), "tier": cfgmod.gemini_tier(),
            "catalogue_size": len(catalogue), "models": rows}


class Command(BaseCommand):
    help = "Verify the configured Gemini model ids exist (lists models only; never generates content)."

    lister: Callable[[], list[dict]] = staticmethod(_default_lister)

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        parser.add_argument("--require-key", action="store_true",
                            help="Fail (exit 2) instead of skipping when no Gemini credentials are configured.")

    def handle(self, *args, **opts):
        if not cfgmod.is_configured():
            msg = ("Gemini is not configured (GEMINI_API_KEY is empty"
                   + (", or GEMINI_VERTEX_PROJECT/LOCATION missing" if cfgmod.gemini_tier() == "vertex" else "")
                   + "): nothing to verify.")
            if opts["require_key"]:
                raise CommandError(msg, returncode=2)
            self.stdout.write(self.style.WARNING(f"SKIPPED  {msg}"))
            return
        try:
            report = verify(type(self).lister)
        except Exception as exc:  # noqa: BLE001 - report any SDK/transport failure clearly
            code = getattr(exc, "kind", None) or type(exc).__name__
            raise CommandError(f"Could not list Gemini models ({code}): {str(exc)[:300]}", returncode=2) from exc
        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Gemini tier: {report['tier']}   models visible: {report['catalogue_size']}")
            for r in report["models"]:
                status = "OK     " if r["found"] and r["can_generate"] else (
                    "MISSING" if not r["found"] else "NO-GEN ")
                limits = (f"  in={r['input_token_limit']} out={r['output_token_limit']}"
                          if r["found"] else "")
                line = f"  {status} {r['setting']:<24} {r['model']}{limits}"
                self.stdout.write(self.style.SUCCESS(line) if status.startswith("OK") else self.style.ERROR(line))
        if not report["ok"]:
            raise CommandError("One or more configured Gemini models are missing or cannot generate content. "
                               "Update GEMINI_MODEL_* in the environment.", returncode=1)
        self.stdout.write(self.style.SUCCESS("All configured Gemini models are available."))
