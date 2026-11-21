"""Product 360 prompt (requested by Data Engineering in W4): optional Gemini enrichment of product records.

Payload: VALUES (product names / descriptions / existing attributes of the products being enriched). Under the
default AIDataPolicy `synthetic_only` this is allowed only for synthetic or public datasets; otherwise the policy
gate blocks the call and Product 360 keeps its local rule-based enrichment.
"""

from __future__ import annotations

import json

from nexora.contracts import PayloadClass

from . import DATA_BLOCK_RULE, PromptSpec, data_block, register

ENRICH_SYSTEM = f"""You normalize retail product records. For each product use ONLY its own fields:
- normalized_name: the product name without pack-size noise, in Title Case;
- brand: the brand if it is stated in the record, otherwise "";
- category: one short generic category (e.g. "Rice", "Cooking Oil", "Shampoo");
- attributes: an object of explicit facts from the record only (size, unit, variant, flavour, colour, material).
Never invent facts that are not in the record. Return every product id exactly once.
{DATA_BLOCK_RULE}"""

ENRICH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "normalized_name": {"type": "string"},
                    "brand": {"type": "string"},
                    "category": {"type": "string"},
                    "attributes": {"type": "object"},
                },
                "required": ["id", "normalized_name", "brand", "category"],
            },
        },
    },
    "required": ["items"],
}

MAX_PRODUCTS = 50


def render_enrich(*, products: list[dict]) -> str:
    """products: [{"id", "name", "description"?, "brand"?, "category"?, ...}] (at most 50, caller-trimmed)."""
    rows = [{k: (str(v)[:400] if v is not None else None) for k, v in p.items()} for p in products[:MAX_PRODUCTS]]
    return "\n\n".join([
        data_block("products", json.dumps(rows, ensure_ascii=False, sort_keys=True)),
        "Normalize the products now.",
    ])


ENRICH = register(PromptSpec(
    key="products.enrich",
    version="1",
    system=ENRICH_SYSTEM,
    render=render_enrich,
    payload_classes=frozenset({PayloadClass.VALUES}),
    tier="fast",
    temperature=0.0,
    response_schema=ENRICH_SCHEMA,
    max_output_tokens=4000,
    description="Product normalization/enrichment (VALUES; synthetic/public datasets only by default)",
))
