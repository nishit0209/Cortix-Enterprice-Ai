"""`bi.narrate` — streamed narration of a query result (payload: SCHEMA + VALUES; policy-gated)."""

from __future__ import annotations

import json

from nexora.contracts import PayloadClass

from . import DATA_BLOCK_RULE, PromptSpec, data_block, register

SYSTEM = f"""You are a concise business analyst. Explain a query result to a business user in 2-4 sentences
of plain English. Use only numbers present in the result or the computed insights; never invent figures.
Do not mention SQL, tables or columns by their technical names unless necessary. No markdown headings.
{DATA_BLOCK_RULE}"""


def render_narrate(*, question: str, columns: list[str], rows: list[list], insights: list[dict]) -> str:
    table = json.dumps({"columns": columns, "rows": rows}, default=str, ensure_ascii=False)
    facts = json.dumps([{"kind": i.get("kind"), "text": i.get("text")} for i in insights], ensure_ascii=False)
    return "\n\n".join([
        data_block("question", question.strip()),
        data_block("result", table),
        data_block("computed_insights", facts),
        "Write the explanation now.",
    ])


NARRATE = register(PromptSpec(
    key="bi.narrate",
    version="1",
    system=SYSTEM,
    render=render_narrate,
    payload_classes=frozenset({PayloadClass.SCHEMA, PayloadClass.VALUES}),
    tier="fast",
    temperature=0.3,
    response_schema=None,
    max_output_tokens=400,
    description="Plain-English narration of a result set",
))
