"""Synthetic QueryPolicy builders for the guard's unit tests (no DB)."""

from __future__ import annotations

import uuid

from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

ORG = uuid.UUID("0190a0c4-0000-7000-8000-00000000a0a1")
USER = uuid.UUID("0190a0c4-0000-7000-8000-00000000a0a2")
DATASET = uuid.UUID("0190a0c4-0000-7000-8000-00000000a0a3")

SALES_COLS = frozenset({"order_id", "order_date", "region", "product", "quantity", "revenue", "customer_id", "meta"})
CUSTOMER_COLS = frozenset({"customer_id", "customer_name", "city", "phone", "email"})


def make_policy(
    *,
    allowed: dict[str, frozenset[str]] | None = None,
    masks: dict[tuple[str, str], MaskSpec] | None = None,
    rows: dict[str, list[tuple]] | None = None,
    row_cap: int = 100,
    org_id: uuid.UUID = ORG,
) -> QueryPolicy:
    allowed = allowed if allowed is not None else {"sales": SALES_COLS, "customers": CUSTOMER_COLS - {"email"}}
    tables = {"sales": "org_unit01.ds_aaaaaaaaaaaa_v1", "customers": "org_unit01.ds_bbbbbbbbbbbb_v2"}
    return QueryPolicy(
        org_id=org_id,
        user_id=USER,
        dataset_id=DATASET,
        tables={t: tables[t] for t in allowed},
        allowed_columns={t: frozenset(c) for t, c in allowed.items()},
        column_masks=masks or {},
        row_predicates={t: RowPredicate(conditions=tuple(conds)) for t, conds in (rows or {}).items()},
        row_cap=row_cap,
        fingerprint="unit-test",
    )
