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
