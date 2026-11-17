"""Cross-agent Python service contracts (Integrator-owned; frozen during a wave).

Implementations:
  PolicyService.effective_policy  -> apps.governance.services.effective_policy   (Backend)
  SqlGuard.validate / execute     -> apps.sqlguard.services.validate / execute     (Data Engineering)
  NL2SQL.propose                  -> apps.nl2sql.services.propose                  (AI/ML)
  GeminiService                   -> apps.ai.services.gemini() (singleton)         (AI/ML)
  AIPolicyGate.check              -> apps.ai.services.check_policy                 (AI/ML)
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID


class AIDataPolicy(StrEnum):
    SYNTHETIC_ONLY = "synthetic_only"  # DEFAULT
    SCHEMA_ONLY = "schema_only"        # requires org owner/admin consent
    AGGREGATES_ONLY = "aggregates_only"  # paid Gemini tier only
    FULL = "full"                      # paid Gemini tier only


class PayloadClass(StrEnum):
    SCHEMA = "SCHEMA"
    VALUES = "VALUES"
    DOCUMENT_TEXT = "DOCUMENT_TEXT"


class AssetClassification(StrEnum):
    SYNTHETIC = "synthetic"
    PUBLIC = "public"
    INTERNAL = "internal"        # default
    CONFIDENTIAL = "confidential"


@dataclass(frozen=True)
class PayloadManifest:
    """What a Gemini prompt contains: payload class -> asset refs ("dataset:<uuid>", "document:<uuid>")."""

    items: Mapping[PayloadClass, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class MaskSpec:
    kind: Literal["mask_null", "mask_hash", "mask_partial"]
    keep_last: int = 4


@dataclass(frozen=True)
class RowPredicate:
    """Structured predicate: all conditions ANDed. op in = != < <= > >= in not_in is_null not_null."""

    conditions: tuple[tuple[str, str, Any], ...]  # (column, op, value)


@dataclass(frozen=True)
class QueryTarget:
    kind: Literal["warehouse", "external"]
    org_id: UUID
    data_source_id: UUID | None = None


@dataclass(frozen=True)
class QueryPolicy:
    org_id: UUID
    user_id: UUID | None
    dataset_id: UUID
    tables: Mapping[str, str]                         # logical table name -> physical "schema.table"
    allowed_columns: Mapping[str, frozenset[str]]     # logical table -> allowed columns
    column_masks: Mapping[tuple[str, str], MaskSpec] = field(default_factory=dict)
    row_predicates: Mapping[str, RowPredicate] = field(default_factory=dict)
    row_cap: int = 1000
    byte_cap: int = 5 * 1024 * 1024
    timeout_s: int = 15
    fingerprint: str = ""


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reject_code: str | None
    message: str
    executed_sql: str | None
    params: Mapping[str, Any]
    tables: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    type: str
    masked: bool = False


@dataclass(frozen=True)
class ResultSet:
    columns: tuple[ColumnMeta, ...]
    rows: tuple[tuple, ...]
    row_count: int
    truncated: bool
    duration_ms: int
    plan_cost: float | None = None


@dataclass(frozen=True)
class ChartHint:
    type: Literal["bar", "line", "area", "pie", "scatter", "table", "kpi"]
    x: str | None = None
    y: tuple[str, ...] = ()
    series: str | None = None


@dataclass(frozen=True)
class QuestionContext:
    org_id: UUID
    user_id: UUID
    dataset_id: UUID
    policy: QueryPolicy
    history: tuple[dict, ...] = ()  # [{"question":..., "sql":..., "columns":[...]}] — NO values
    # logical table -> [{"name","type","semantic_type","role","description","synonyms"}] (allowed columns only)
    columns_meta: Mapping[str, tuple[dict, ...]] = field(default_factory=dict)
    metrics: tuple[dict, ...] = ()  # [{"key","name","expression","description","synonyms"}]
    dataset_name: str = ""
    dataset_classification: str = "internal"


@dataclass(frozen=True)
class SqlProposal:
    sql: str | None
    source: Literal["cache_exact", "cache_semantic", "builder", "llm", "none"]
    explanation: str | None = None
    assumptions: tuple[str, ...] = ()
    chart_hint: ChartHint | None = None
    clarification: str | None = None
    ai_request_ids: tuple[UUID, ...] = ()
    blocked_reason: str | None = None  # e.g. "ai_policy_blocked", "ai_quota_exhausted", "ai_not_configured"


class PolicyService(Protocol):
    def effective_policy(self, user_id: UUID, dataset_id: UUID) -> QueryPolicy: ...


class SqlGuard(Protocol):
    def validate(self, sql: str, policy: QueryPolicy) -> GuardResult: ...
    def execute(self, result: GuardResult, policy: QueryPolicy, target: QueryTarget) -> ResultSet: ...


class NL2SQL(Protocol):
    def propose(self, question: str, ctx: QuestionContext) -> SqlProposal: ...


class GeminiService(Protocol):
    def generate_structured(self, *, feature: str, prompt_key: str, system: str, prompt: str,
                            schema: dict, payload: PayloadManifest, tier: str = "fast") -> dict: ...
    def stream(self, *, feature: str, prompt_key: str, system: str, prompt: str,
               payload: PayloadManifest, tier: str = "fast") -> Iterator[str]: ...
