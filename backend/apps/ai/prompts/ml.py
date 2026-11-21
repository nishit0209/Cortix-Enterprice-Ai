"""ML workspace prompts. Optional narration of EDA / experiment results for business readers.

Payload: SCHEMA (column names, task, metric names) and, only when the org's AI data policy allows it, VALUES
(aggregates: class shares, correlations, metric values). Never rows. The default narration is a local template
(apps.ml.engine.eda.template_narrative); Gemini is used only through GeminiService and the policy gate.
"""

from __future__ import annotations

import json

from nexora.contracts import PayloadClass

from . import DATA_BLOCK_RULE, PromptSpec, data_block, register

NARRATE_SYSTEM = f"""You explain machine-learning data analysis to a business reader in plain English.
Use ONLY the supplied facts (aggregates and column names). Never invent numbers. 3-5 short sentences, no
markdown, no bullet points. Mention data-quality risks (imbalance, missing values, small data) when present.
{DATA_BLOCK_RULE}"""


def render_narrate(*, facts: dict, kind: str = "eda") -> str:
    return "\n\n".join([
        data_block("facts", json.dumps(facts, sort_keys=True, default=str)[:6000]),
        f"Write the {kind} explanation now.",
    ])


NARRATE = register(PromptSpec(
    key="ml.narrate",
    version="1",
    system=NARRATE_SYSTEM,
    render=render_narrate,
    payload_classes=frozenset({PayloadClass.SCHEMA, PayloadClass.VALUES}),
    tier="fast",
    temperature=0.2,
    max_output_tokens=400,
    description="Plain-English narration of ML EDA / experiment aggregates",
))
