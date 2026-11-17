"""Synthetic sales schema + QuestionContext factory for nl2sql tests (no real data)."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from nexora.contracts import QueryPolicy, QuestionContext

TODAY = date(2026, 11, 17)
ORG = UUID("0190a0c4-0000-7000-8000-00000000a001")
USER = UUID("0190a0c4-0000-7000-8000-00000000b001")
DATASET = UUID("0190a0c4-0000-7000-8000-00000000d001")

SALES_COLUMNS = (
    {"name": "order_id", "type": "bigint", "role": "identifier", "semantic_type": "id"},
    {"name": "order_date", "type": "date", "role": "time"},
    {"name": "region", "type": "text", "role": "dimension", "synonyms": ["zone", "territory"]},
    {"name": "product_category", "type": "text", "role": "dimension", "synonyms": ["category"]},
    {"name": "channel", "type": "text", "role": "dimension"},
    {"name": "customer_id", "type": "bigint", "role": "identifier"},
    {"name": "quantity", "type": "integer", "role": "measure", "synonyms": ["units", "units sold"]},
    {"name": "amount", "type": "numeric(12,2)", "role": "measure", "synonyms": ["sales", "revenue"],
     "description": "Net invoice amount in INR"},
    {"name": "discount", "type": "numeric(12,2)", "role": "measure"},
    {"name": "customer_email", "type": "text", "semantic_type": "email", "role": "dimension"},
)
METRICS = (
    {"key": "aov", "name": "average order value", "expression": "SUM(amount) / NULLIF(COUNT(DISTINCT order_id), 0)",
     "description": "Revenue per order", "synonyms": ["basket size"]},
    {"key": "gross_margin", "name": "gross margin", "expression": "SUM(amount) - SUM(discount)"},
)


def make_ctx(*, allowed: set[str] | None = None, columns=SALES_COLUMNS, metrics=METRICS, history=(),
             tables: dict[str, str] | None = None, columns_meta=None) -> QuestionContext:
    tables = tables or {"sales": "org_test.ds_1_v1"}
    if columns_meta is None:
        columns_meta = {"sales": tuple(columns)}
    names = {c["name"] for cols in columns_meta.values() for c in cols}
    allowed_cols = {t: frozenset(allowed if allowed is not None else
                                 {c["name"] for c in columns_meta.get(t, ())}) for t in tables}
    if allowed is not None:
        assert allowed <= names
    policy = QueryPolicy(org_id=ORG, user_id=USER, dataset_id=DATASET, tables=tables, allowed_columns=allowed_cols,
                         fingerprint="fp-test")
    return QuestionContext(org_id=ORG, user_id=USER, dataset_id=DATASET, policy=policy, history=tuple(history),
                           columns_meta=columns_meta, metrics=tuple(metrics), dataset_name="Synthetic Sales",
                           dataset_classification="internal")


@pytest.fixture
def ctx() -> QuestionContext:
    return make_ctx()
