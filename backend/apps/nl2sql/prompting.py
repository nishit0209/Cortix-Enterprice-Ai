"""Builds the SCHEMA-only inputs of the nl2sql prompts from a QuestionContext.

* Only allowed columns (ctx.columns_meta ∩ ctx.policy.allowed_columns) are listed.
* PII-tagged columns are ALIASED (pii_col_N) — their real names never reach Gemini; aliases in the model's
  SQL are mapped back locally.
* The conversation summary keeps previous questions, value-free SQL shapes and column lists. NO values.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nexora.contracts import QuestionContext

from .sqlutil import shape_without_literals

PII_SEMANTIC_TYPES = frozenset({
    "email", "phone", "phone_number", "mobile", "person_name", "name", "full_name", "first_name", "last_name",
    "address", "street_address", "postal_address", "pan", "aadhaar", "ssn", "national_id", "passport",
    "credit_card", "card_number", "iban", "bank_account", "ip_address", "dob", "date_of_birth", "pii",
})
MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 2800  # ~800 tokens


def is_pii(col: dict) -> bool:
    if col.get("is_pii") or col.get("pii"):
        return True
    return str(col.get("semantic_type") or "").lower() in PII_SEMANTIC_TYPES


@dataclass
class SchemaCard:
    tables: list[dict] = field(default_factory=list)
    alias_to_real: dict[str, str] = field(default_factory=dict)  # pii_col_1 -> customer_email
    real_to_alias: dict[str, str] = field(default_factory=dict)

    @property
    def table_names(self) -> list[str]:
        return [t["name"] for t in self.tables]


def build_schema_card(ctx: QuestionContext) -> SchemaCard:
    card = SchemaCard()
    counter = 0
    for table in ctx.policy.tables:
        allowed = {c.lower() for c in ctx.policy.allowed_columns.get(table, frozenset())}
        cols_out: list[dict] = []
        for col in (ctx.columns_meta or {}).get(table, ()):
            name = str(col.get("name") or "")
            if not name or name.lower() not in allowed:
                continue  # fail closed: only explicitly allowed columns
            if is_pii(col):
                counter += 1
                alias = f"pii_col_{counter}"
                card.alias_to_real[alias] = name
                card.real_to_alias[name] = alias
                cols_out.append({"name": alias, "type": col.get("type") or "text",
                                 "semantic_type": str(col.get("semantic_type") or "pii"),
                                 "role": col.get("role") or "", "description": "PII column (name withheld)"})
                continue
            cols_out.append({
                "name": name,
                "type": col.get("type") or "text",
                "semantic_type": col.get("semantic_type") or "",
                "role": col.get("role") or "",
                "description": str(col.get("description") or "")[:200],
                "synonyms": [str(s) for s in (col.get("synonyms") or ())][:6],
            })
        card.tables.append({"name": table, "columns": cols_out})
    return card


def metric_defs(ctx: QuestionContext, card: SchemaCard) -> list[dict]:
    out = []
    for m in ctx.metrics or ():
        expr = str(m.get("expression") or "")
        if not expr:
            continue
        for real, alias in card.real_to_alias.items():
            expr = _replace_ident(expr, real, alias)
        out.append({"key": m.get("key"), "name": m.get("name"), "expression": expr,
                    "description": str(m.get("description") or "")[:200],
                    "synonyms": [str(s) for s in (m.get("synonyms") or ())][:6]})
    return out


def _replace_ident(expr: str, real: str, alias: str) -> str:
    import re

    return re.sub(rf'(?<![\w"])"?{re.escape(real)}"?(?![\w"])', f'"{alias}"', expr, flags=re.I)


def history_summary(ctx: QuestionContext, card: SchemaCard) -> list[dict]:
    """Last turns: question + value-free SQL shape + columns (PII names aliased). Never values."""
    from .sqlutil import parse_select, rename_identifiers, render

    turns: list[dict] = []
    total = 0
    for h in list(ctx.history or ())[-MAX_HISTORY_TURNS:]:
        sql = str(h.get("sql") or "")
        shape = ""
        if sql:
            ast, issue = parse_select(sql)
            if not issue:
                shape = shape_without_literals(render(rename_identifiers(ast, card.real_to_alias)))
        cols = [card.real_to_alias.get(str(c), str(c)) for c in (h.get("columns") or [])][:20]
        item = {"question": str(h.get("question") or "")[:300], "sql_shape": shape[:600], "columns": cols}
        size = len(item["question"]) + len(item["sql_shape"]) + sum(len(c) for c in cols)
        if total + size > MAX_HISTORY_CHARS:
            break
        turns.append(item)
        total += size
    return turns
