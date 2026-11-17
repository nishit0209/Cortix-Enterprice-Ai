"""Versioned AI data-policy consent text.

The text shown to (and acknowledged by) an owner/admin is identified by
`settings.AI_CONSENT_TEXT_VERSION`. Bumping that setting invalidates every existing consent: orgs fall
back to synthetic_only until an owner/admin re-consents to the new text.
"""

from __future__ import annotations

from django.conf import settings

POLICY_DESCRIPTIONS = {
    "synthetic_only": "Only datasets and documents classified as synthetic or public are ever sent to Google Gemini.",
    "schema_only": (
        "Schema metadata of your datasets (table names, column names, data types and semantic types, never "
        "cell values) may be sent to Google Gemini to generate SQL. Data classified synthetic or public may "
        "still be sent in full."
    ),
    "aggregates_only": (
        "Schema metadata and aggregated query results (no row-level values) may be sent to Google Gemini. "
        "Requires a paid Gemini tier."
    ),
    "full": (
        "Schema metadata, query results and document text may be sent to Google Gemini. Requires a paid "
        "Gemini tier."
    ),
}

_TEMPLATE = """NEXORA AI data-processing consent (version {version})

By granting this consent on behalf of your organization you confirm that:

1. You are an owner or administrator of the organization and are authorised to decide how its data is processed.
2. NEXORA uses Google Gemini as its only external generative-AI provider. Data sent to Gemini leaves NEXORA's
   infrastructure and is processed by Google under the terms of the configured Gemini tier ({tier}).
3. On the Gemini free tier, Google may use submitted content to improve its products. Never grant a policy
   above "synthetic_only" for real personal or confidential data while the free tier is configured.
4. The selected policy determines what may be sent:
   - synthetic_only: {synthetic_only}
   - schema_only: {schema_only}
   - aggregates_only: {aggregates_only}
   - full: {full}
5. Datasets and documents classified "internal" or "confidential" are never sent beyond what the active policy
   allows, and every Gemini request is logged with its payload classes for audit.
6. You can revoke this consent at any time; the organization immediately returns to "synthetic_only".
7. The consent is recorded with your identity, IP address, user agent and timestamp in an append-only ledger.
"""


def current_version() -> str:
    return settings.AI_CONSENT_TEXT_VERSION


def consent_text(version: str | None = None) -> str:
    return _TEMPLATE.format(version=version or current_version(), tier=settings.GEMINI_TIER, **POLICY_DESCRIPTIONS)
