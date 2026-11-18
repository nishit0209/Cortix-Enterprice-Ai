"""The ONLY place apps.bi reaches into other agents' apps. Everything goes through their `services.py` and the
frozen contracts in `nexora.contracts`; functions are resolved at call time so tests can substitute fakes
(`monkeypatch.setattr(apps.bi.ports, "validate", fake)`).

    semantic   (Data Engineering) dataset_semantics(dataset_id) -> DatasetSemantics
    sqlguard   (Data Engineering) validate(sql, policy) -> GuardResult; execute(guard, policy, target) -> ResultSet
    nl2sql     (AI/ML)            propose / repair / remember / store_verified / compute_insights / narrate
    governance (Backend)          check_dataset_access / visible_dataset_ids / effective_policy / record_access
"""

from __future__ import annotations

import logging
from uuid import UUID

from nexora.contracts import (
    DatasetSemantics,
    GuardResult,
    QueryPolicy,
    QueryTarget,
    QuestionContext,
    ResultSet,
    SqlProposal,
)

log = logging.getLogger("nexora.bi")


# --- semantic layer ----------------------------------------------------------------------------------------
def dataset_semantics(dataset_id: UUID) -> DatasetSemantics:
    from apps.semantic import services

    return services.dataset_semantics(dataset_id)


# --- SQL guard -----------------------------------------------------------------------------------------------
def validate(sql: str, policy: QueryPolicy) -> GuardResult:
    from apps.sqlguard import services

    return services.validate(sql, policy)


def execute(guard: GuardResult, policy: QueryPolicy, target: QueryTarget) -> ResultSet:
    from apps.sqlguard import services

    return services.execute(guard, policy, target)


def executing_role(org_id: UUID) -> str:
    """The warehouse reader role queries run as (evidence for the audit row; never fails the query)."""
    try:
        from apps.warehouse import services

        return str(services.reader_role(org_id))
    except Exception:  # noqa: BLE001 - best effort
        return ""


# --- NL -> SQL --------------------------------------------------------------------------------------------------
def propose(question: str, ctx: QuestionContext, *, dataset_version_id: UUID | None = None) -> SqlProposal:
    from apps.nl2sql import services

    return services.propose(question, ctx, dataset_version_id=dataset_version_id)


def repair(question: str, bad_sql: str, reject_code: str, ctx: QuestionContext, *, reject_message: str = "") -> SqlProposal:
    from apps.nl2sql import services

    return services.repair(question, bad_sql, reject_code, ctx, reject_message=reject_message)


def remember(question: str, sql: str, ctx: QuestionContext, **kwargs):
    from apps.nl2sql import services

    return services.remember(question, sql, ctx, **kwargs)


def store_verified(question: str, sql: str, ctx: QuestionContext, **kwargs):
    from apps.nl2sql import services

    return services.store_verified(question, sql, ctx, **kwargs)


def compute_insights(columns, rows) -> list[dict]:
    from apps.nl2sql import services

    return services.compute_insights(columns, rows)


def narrate(question: str, insights: list[dict], *, row_count: int | None = None) -> str:
    """Local template narration (no LLM)."""
    from apps.nl2sql import insights as nl2sql_insights

    return nl2sql_insights.narrate_template(question, insights, row_count=row_count)


def blocked_message(code: str) -> str | None:
    from apps.nl2sql import services

    return getattr(services, "BLOCKED_MESSAGES", {}).get(code)


# --- governance ------------------------------------------------------------------------------------------------
def check_dataset_access(user_id: UUID, dataset_id: UUID, privilege: str) -> bool:
    from apps.governance import services

    return services.check_dataset_access(user_id, dataset_id, privilege)


def visible_dataset_ids(user_id: UUID, dataset_ids, privilege: str = "read") -> set[str]:
    from apps.governance import services

    return services.visible_dataset_ids(user_id, dataset_ids, privilege)


def effective_policy(user_id: UUID, dataset_id: UUID, tables, columns_by_table, *, privilege: str) -> QueryPolicy:
    from apps.governance import services

    return services.effective_policy(user_id, dataset_id, tables, columns_by_table, privilege=privilege)


def record_access(asset_id: UUID, access_type: str, *, user_id, api_key_id=None, query_audit_id=None,
                  metadata: dict | None = None) -> None:
    from apps.governance import services

    services.record_access("dataset", asset_id, access_type, user_id=user_id, api_key_id=api_key_id,
                           query_audit_id=query_audit_id, metadata=metadata)
