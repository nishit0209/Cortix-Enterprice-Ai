"""Synthetic fakes for the other agents' services that apps.bi calls through `apps.bi.ports`.

`install(monkeypatch)` swaps semantic / sqlguard / nl2sql (and optionally governance) for in-memory fakes and
returns a `FakeBackends` that records every call. No network, no Gemini, no warehouse.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from apps.bi import ports
from apps.core.exceptions import NexoraError
from nexora.contracts import (
    ChartHint,
    ColumnMeta,
    DatasetSemantics,
    GuardResult,
    MaskSpec,
    QueryPolicy,
    ResultSet,
    SqlProposal,
)

DATASET_ID = uuid.UUID("01930000-0000-7000-8000-00000000d5a1")
DATASET_VERSION_ID = uuid.UUID("01930000-0000-7000-8000-00000000d5a2")
UNKNOWN_DATASET_ID = uuid.UUID("01930000-0000-7000-8000-00000000dead")

SALES_COLUMNS = ("region", "amount", "order_date", "customer_email", "margin")


def make_semantics(dataset_id: uuid.UUID = DATASET_ID) -> DatasetSemantics:
    return DatasetSemantics(
        dataset_id=dataset_id,
        dataset_version_id=DATASET_VERSION_ID,
        dataset_name="Synthetic sales",
        classification="synthetic",
        tables={"sales": "t_backend_ab12.ds_d5a1_v1"},
        columns_by_table={"sales": SALES_COLUMNS},
        columns_meta={"sales": (
            {"name": "region", "type": "text", "semantic_type": "category", "role": "dimension"},
            {"name": "amount", "type": "numeric", "semantic_type": "currency", "role": "measure"},
            {"name": "order_date", "type": "date", "semantic_type": "date", "role": "time"},
            {"name": "customer_email", "type": "text", "semantic_type": "email", "role": "attribute"},
            {"name": "margin", "type": "numeric", "semantic_type": "currency", "role": "measure"},
        )},
        metrics=(
            {"key": "revenue", "name": "Revenue", "expression": "SUM(amount)"},
            {"key": "margin_total", "name": "Margin", "expression": "SUM(margin)"},
        ),
        schema_hash="fake",
    )


def make_policy(*, org_id=None, user_id=None, dataset_id=DATASET_ID, deny=("margin",),
                mask=("customer_email",)) -> QueryPolicy:
    allowed = frozenset(c for c in SALES_COLUMNS if c not in deny)
    return QueryPolicy(
        org_id=org_id or uuid.uuid4(),
        user_id=user_id,
        dataset_id=dataset_id,
        tables={"sales": "t_backend_ab12.ds_d5a1_v1"},
        allowed_columns={"sales": allowed},
        column_masks={("sales", c): MaskSpec(kind="mask_hash") for c in mask},
        fingerprint="f" * 64,
    )


REGION_RESULT = ResultSet(
    columns=(ColumnMeta("region", "text"), ColumnMeta("revenue", "numeric")),
    rows=(("West", Decimal("5400.50")), ("North", Decimal("4100")), ("South", Decimal("2500")),
          ("East", Decimal("900.25"))),
    row_count=4,
    truncated=False,
    duration_ms=12,
    plan_cost=42.0,
)

DANGEROUS_RESULT = ResultSet(
    columns=(ColumnMeta("label", "text"), ColumnMeta("value", "numeric"), ColumnMeta("day", "date")),
    rows=(("=HYPERLINK(\"http://x.test\",\"x\")", Decimal("-12.5"), date(2026, 1, 5)),
          ("+cmd|' /C calc'!A0", 3, date(2026, 1, 6)),
          ("@SUM(1+1)", -4, None),
          ("\tTabbed", 1.5, date(2026, 1, 7)),
          ("-2+3", 0, date(2026, 1, 8)),
          ("plain", 7, date(2026, 1, 9))),
    row_count=6,
    truncated=False,
    duration_ms=5,
)


class FakeQueryError(NexoraError):
    status_code = 422
    default_code = "execution_error"
    default_detail = "column reference is ambiguous"


class FakeTimeout(NexoraError):
    status_code = 504
    default_code = "query_timeout"
    default_detail = "The query took too long."


@dataclass
class FakeBackends:
    semantics: dict = field(default_factory=lambda: {DATASET_ID: make_semantics()})
    proposals: list = field(default_factory=list)  # consumed by propose()
    repairs: list = field(default_factory=list)  # consumed by repair()
    rejections: dict = field(default_factory=dict)  # sql -> (code, message)
    exec_errors: dict = field(default_factory=dict)  # executed sql -> exception
    result: ResultSet = REGION_RESULT
    calls: list = field(default_factory=list)
    remembered: list = field(default_factory=list)
    verified: list = field(default_factory=list)
    verify_error: Exception | None = None
    acl: dict = field(default_factory=dict)  # dataset_id -> bool (governance fake only)

    # semantic
    def dataset_semantics(self, dataset_id):
        self.calls.append(("semantics", dataset_id))
        try:
            return self.semantics[dataset_id]
        except KeyError:
            raise LookupError(dataset_id) from None

    # sqlguard
    def validate(self, sql, policy):
        self.calls.append(("validate", sql))
        if sql in self.rejections:
            code, message = self.rejections[sql]
            return GuardResult(ok=False, reject_code=code, message=message, executed_sql=None, params={})
        executed = f"SELECT * FROM ({sql}) AS guarded LIMIT {policy.row_cap}"
        return GuardResult(ok=True, reject_code=None, message="", executed_sql=executed, params={"p0": 1},
                           tables=tuple(policy.tables))

    def execute(self, guard, policy, target):
        self.calls.append(("execute", guard.executed_sql, target))
        assert target.kind == "warehouse" and target.org_id == policy.org_id
        for raw, exc in self.exec_errors.items():
            if raw in (guard.executed_sql or ""):
                raise exc
        return self.result

    # nl2sql
    def propose(self, question, ctx, *, dataset_version_id=None):
        self.calls.append(("propose", question, ctx, dataset_version_id))
        if self.proposals:
            return self.proposals.pop(0)
        return SqlProposal(sql="SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region", source="builder",
                           explanation="Revenue by region.", assumptions=("All time.",),
                           chart_hint=ChartHint(type="bar", x="region", y=("revenue",)))

    def repair(self, question, bad_sql, reject_code, ctx, *, reject_message=""):
        self.calls.append(("repair", bad_sql, reject_code))
        if self.repairs:
            return self.repairs.pop(0)
        return SqlProposal(sql=None, source="none", blocked_reason="ai_policy_blocked")

    def remember(self, question, sql, ctx, **kwargs):
        self.remembered.append((question, sql, kwargs))

    def store_verified(self, question, sql, ctx, **kwargs):
        if self.verify_error is not None:
            raise self.verify_error
        self.verified.append((question, sql, ctx, kwargs))

    # governance (pure tests only)
    def check_dataset_access(self, user_id, dataset_id, privilege):
        self.calls.append(("acl", dataset_id, privilege))
        return self.acl.get(dataset_id, True)

    def effective_policy(self, user_id, dataset_id, tables, columns_by_table, *, privilege):
        self.calls.append(("policy", dataset_id, privilege))
        return make_policy(user_id=user_id, dataset_id=dataset_id, org_id=self.org_id)

    org_id: Any = None

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def install(monkeypatch, *, governance: bool = False, org_id=None) -> FakeBackends:
    fake = FakeBackends()
    fake.org_id = org_id
    for name in ("dataset_semantics", "validate", "execute", "propose", "repair", "remember", "store_verified"):
        monkeypatch.setattr(ports, name, getattr(fake, name))
    monkeypatch.setattr(ports, "executing_role", lambda org_id: "wh_q_fake")
    if governance:
        monkeypatch.setattr(ports, "check_dataset_access", fake.check_dataset_access)
        monkeypatch.setattr(ports, "effective_policy", fake.effective_policy)
        monkeypatch.setattr(ports, "record_access", lambda *a, **k: fake.calls.append(("access", a, k)))
    return fake
