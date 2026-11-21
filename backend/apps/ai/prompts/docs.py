"""Document intelligence prompts (AI_ARCHITECTURE.md §7). Payload: DOCUMENT_TEXT on document:<id> (policy-gated).

* `docs.answer`    — grounded Q&A over retrieved chunks, cited as [S<n>] with verbatim quotes.
* `docs.summarize` — short summary of a document built ONLY from the supplied passages.
"""

from __future__ import annotations

from nexora.contracts import PayloadClass

from . import DATA_BLOCK_RULE, PromptSpec, data_block, register

NOT_FOUND = "Not found in the provided documents."

ANSWER_SYSTEM = f"""You answer questions using ONLY the provided sources. Each source is a <source id="S<n>">
block taken from a document page. Rules:
- Use only facts stated in the sources. Never use outside knowledge and never guess.
- Every claim must be supported by at least one citation. For each citation give the source id and a SHORT
  quote (at most 30 words) copied VERBATIM from that source.
- If the sources do not contain the answer, set unanswerable=true and answer exactly "{NOT_FOUND}".
- Answer in the language of the question. Be concise (at most 6 sentences). No markdown headings.
{DATA_BLOCK_RULE}"""

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["source_id", "quote"],
            },
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "unanswerable": {"type": "boolean"},
    },
    "required": ["answer", "citations", "confidence", "unanswerable"],
}


def render_sources(sources: list[dict]) -> str:
    """sources: [{"source_id": "S1", "document": title, "page": n, "text": ...}] (already budgeted)."""
    parts = []
    for s in sources:
        title = str(s.get("document", "")).replace('"', "'")[:120]
        body = str(s.get("text", "")).replace("</source>", "</ source>")
        parts.append(f'<source id="{s["source_id"]}" doc="{title}" page="{s.get("page")}">\n{body}\n</source>')
    return "\n".join(parts)


def render_answer(*, question: str, sources: list[dict]) -> str:
    return "\n\n".join([
        data_block("sources", render_sources(sources)),
        data_block("question", question.strip()),
        "Answer the question from the sources now.",
    ])


ANSWER = register(PromptSpec(
    key="docs.answer",
    version="1",
    system=ANSWER_SYSTEM,
    render=render_answer,
    payload_classes=frozenset({PayloadClass.DOCUMENT_TEXT}),
    tier="fast",
    temperature=0.0,
    response_schema=ANSWER_SCHEMA,
    max_output_tokens=800,
    description="Grounded document Q&A with verbatim-quote citations",
))

SUMMARY_SYSTEM = f"""You summarize a document for a business reader using ONLY the supplied passages.
Write 3-6 short bullet-free sentences covering purpose, parties, key figures and dates that appear in the
passages. Never invent facts. Keep the document's language.
{DATA_BLOCK_RULE}"""

SUMMARY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["summary"],
}


def render_summary(*, title: str, sources: list[dict]) -> str:
    return "\n\n".join([
        data_block("title", title),
        data_block("passages", render_sources(sources)),
        "Summarize the document now.",
    ])


SUMMARY = register(PromptSpec(
    key="docs.summarize",
    version="1",
    system=SUMMARY_SYSTEM,
    render=render_summary,
    payload_classes=frozenset({PayloadClass.DOCUMENT_TEXT}),
    tier="fast",
    temperature=0.2,
    response_schema=SUMMARY_SCHEMA,
    max_output_tokens=600,
    description="Summary of a document from its passages",
))

EXTRACT_SYSTEM = f"""You extract structured fields from a document using ONLY the provided sources. Each source is a
<source id="S<n>"> block taken from a document page. Rules:
- Return one entry per requested field. Copy the value exactly as written in the source (no reformatting).
- For each value give the source id and a SHORT quote (at most 20 words) copied VERBATIM from that source that
  contains the value.
- If a field is not stated in the sources, return it with value "" and no quote. Never guess or infer.
{DATA_BLOCK_RULE}"""

EXTRACT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "source_id": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "value"],
            },
        },
    },
    "required": ["fields"],
}


def render_extract(*, schema_label: str, fields: list[dict], sources: list[dict]) -> str:
    wanted = "\n".join(f"- {f['name']} ({f['type']}): {f['description']}" for f in fields)
    return "\n\n".join([
        data_block("document_type", schema_label),
        data_block("fields", wanted),
        data_block("sources", render_sources(sources)),
        "Extract the fields from the sources now.",
    ])


EXTRACT = register(PromptSpec(
    key="docs.extract",
    version="1",
    system=EXTRACT_SYSTEM,
    render=render_extract,
    payload_classes=frozenset({PayloadClass.DOCUMENT_TEXT}),
    tier="fast",
    temperature=0.0,
    response_schema=EXTRACT_SCHEMA,
    max_output_tokens=1200,
    description="Schema-driven field extraction with verbatim-quote citations (results need review)",
))

COMPARE_SYSTEM = f"""You explain the differences between two versions of a document for a business reader, using
ONLY the supplied section diff. Write 2-5 short sentences on what changed (amounts, dates, parties, obligations).
Never invent changes that are not in the diff.
{DATA_BLOCK_RULE}"""

COMPARE_SCHEMA: dict = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "key_changes": {"type": "array", "items": {"type": "string"},
                                                                 "maxItems": 8}},
    "required": ["summary"],
}


def render_compare(*, title_a: str, title_b: str, diff: str) -> str:
    return "\n\n".join([
        data_block("document_a", title_a), data_block("document_b", title_b), data_block("diff", diff),
        "Summarize the differences now.",
    ])


COMPARE = register(PromptSpec(
    key="docs.compare",
    version="1",
    system=COMPARE_SYSTEM,
    render=render_compare,
    payload_classes=frozenset({PayloadClass.DOCUMENT_TEXT}),
    tier="fast",
    temperature=0.2,
    response_schema=COMPARE_SCHEMA,
    max_output_tokens=600,
    description="Summary of a local section diff between two documents",
))
