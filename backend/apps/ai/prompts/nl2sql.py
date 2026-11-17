"""NL->SQL prompts: `nl2sql.generate` and `nl2sql.repair` (payload: SCHEMA only).

The prompt contains ONLY: the schema card (allowed columns, PII columns aliased), metric definitions, a
history summary WITHOUT values, the rules, today's date, and the user's question. Never data values.
"""

from __future__ import annotations

import json

from nexora.contracts import PayloadClass

from . import DATA_BLOCK_RULE, PromptSpec, data_block, register

CHART_TYPES = ["bar", "line", "area", "pie", "scatter", "table", "kpi"]

NL2SQL_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "sql": {"type": "string", "nullable": True,
                "description": "One PostgreSQL SELECT statement, or null when clarification is needed."},
        "explanation": {"type": "string", "description": "One or two plain-English sentences."},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "chart_hint": {
            "type": "object",
            "nullable": True,
            "properties": {
                "type": {"type": "string", "enum": CHART_TYPES},
                "x": {"type": "string", "nullable": True},
                "y": {"type": "array", "items": {"type": "string"}},
                "series": {"type": "string", "nullable": True},
            },
            "required": ["type"],
        },
        "clarification": {"type": "string", "nullable": True,
                          "description": "A question for the user when the request is ambiguous; else null."},
        "tables": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sql", "explanation", "assumptions", "chart_hint", "clarification"],
}

SYSTEM = f"""You translate business questions into ONE read-only PostgreSQL query for a BI tool.
Rules:
1. Output a single SELECT statement (WITH/CTEs allowed). Never INSERT, UPDATE, DELETE, DDL, COPY, SET or comments.
2. PostgreSQL dialect. Use ONLY the tables and columns listed in the schema card, with double-quoted identifiers.
3. Never invent columns, tables or values. Do not use system catalogs or administrative functions.
4. Always include LIMIT n with n <= 1000 and never above the maximum given in the request (use a smaller LIMIT for "top N" questions).
5. Prefer the listed metric expressions when the question mentions a metric.
6. Resolve relative dates against today's date given in the request, with explicit date literals.
7. If the question is ambiguous or cannot be answered from the schema, set "sql" to null and ask one short
   clarifying question in "clarification".
8. "chart_hint" must reference output column names of your query.
{DATA_BLOCK_RULE}
Respond with JSON only, matching the response schema."""


def _schema_card(tables: list[dict]) -> str:
    lines: list[str] = []
    for t in tables:
        lines.append(f'TABLE "{t["name"]}"')
        for c in t.get("columns", []):
            bits = [f'  "{c["name"]}" {c.get("type") or "text"}']
            if c.get("role"):
                bits.append(f"role={c['role']}")
            if c.get("semantic_type"):
                bits.append(f"semantic={c['semantic_type']}")
            if c.get("synonyms"):
                bits.append("aka=" + "|".join(str(s) for s in c["synonyms"][:6]))
            if c.get("description"):
                bits.append("-- " + str(c["description"])[:160])
            lines.append(" ".join(bits))
    return "\n".join(lines)


def _metrics(metrics: list[dict]) -> str:
    if not metrics:
        return "(none)"
    return "\n".join(
        f'- {m.get("key") or m.get("name")}: {m.get("expression")}'
        + (f' -- {m["description"][:160]}' if m.get("description") else "")
        + (f' (aka {"|".join(m["synonyms"][:6])})' if m.get("synonyms") else "")
        for m in metrics
    )


def _history(history: list[dict]) -> str:
    if not history:
        return "(no previous turns)"
    out = []
    for i, h in enumerate(history, 1):
        cols = ", ".join(h.get("columns") or [])
        out.append(f"{i}. Q: {h.get('question', '')}\n   SQL shape: {h.get('sql_shape', '')}\n   Columns: {cols}")
    return "\n".join(out)


def render_generate(*, dataset_name: str, tables: list[dict], metrics: list[dict], history: list[dict],
                    question: str, today: str, row_limit: int = 1000) -> str:
    return "\n\n".join([
        f"Today's date: {today}. Dataset: {dataset_name or 'dataset'}. Maximum LIMIT: {row_limit}.",
        data_block("schema_card", _schema_card(tables)),
        data_block("metrics", _metrics(metrics)),
        data_block("conversation_summary", _history(history)),
        data_block("question", question.strip()),
        "Return the JSON object now.",
    ])


def render_repair(*, dataset_name: str, tables: list[dict], metrics: list[dict], history: list[dict],
                  question: str, today: str, bad_sql: str, reject_code: str, reject_message: str = "",
                  row_limit: int = 1000) -> str:
    return "\n\n".join([
        f"Today's date: {today}. Dataset: {dataset_name or 'dataset'}. Maximum LIMIT: {row_limit}.",
        data_block("schema_card", _schema_card(tables)),
        data_block("metrics", _metrics(metrics)),
        data_block("question", question.strip()),
        data_block("rejected_sql", bad_sql.strip()),
        data_block("rejection", json.dumps({"code": reject_code, "message": reject_message[:300]})),
        "The rejected SQL above failed validation. Write a corrected query that follows every rule and uses only "
        "the schema card. Return the JSON object now.",
    ])


GENERATE = register(PromptSpec(
    key="nl2sql.generate",
    version="1",
    system=SYSTEM,
    render=render_generate,
    payload_classes=frozenset({PayloadClass.SCHEMA}),
    tier="fast",
    temperature=0.0,
    response_schema=NL2SQL_RESPONSE_SCHEMA,
    max_output_tokens=1024,
    description="Question -> SQL + explanation + chart hint in one call",
))

REPAIR = register(PromptSpec(
    key="nl2sql.repair",
    version="1",
    system=SYSTEM,
    render=render_repair,
    payload_classes=frozenset({PayloadClass.SCHEMA}),
    tier="reasoning",
    temperature=0.0,
    response_schema=NL2SQL_RESPONSE_SCHEMA,
    max_output_tokens=1024,
    description="One repair attempt after a non-security validation rejection",
))
