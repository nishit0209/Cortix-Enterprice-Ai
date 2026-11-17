"""Parsed text table -> typed frame + column metadata + profile (schema detection, PII, profiling)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from .columns import normalize_column_names
from .errors import IngestError
from .inference import ColumnTyping, infer_table, schema_hash
from .parsing import ParsedTable
from .profiling import profile_column, profile_dataset
from .semantics import detect_pii, detect_semantic


@dataclass
class ColumnResult:
    ordinal: int
    name: str
    source_name: str
    data_type: str
    semantic_type: str | None
    nullable: bool
    is_pii: bool
    pii_type: str | None
    pii_source: str | None
    invalid_count: int
    stats: dict[str, Any]


@dataclass
class AnalyzedTable:
    frame: pl.DataFrame
    columns: list[ColumnResult]
    profile: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> list[tuple[str, str]]:
        return [(c.name, c.data_type) for c in self.columns]

    @property
    def schema_hash(self) -> str:
        return schema_hash(self.schema)

    @property
    def pii_columns(self) -> set[str]:
        return {c.name for c in self.columns if c.is_pii}


def _column_result(typing: ColumnTyping, frame: pl.DataFrame) -> ColumnResult:
    series = frame[typing.name]
    pii = detect_pii(typing.name, typing.sample)
    semantic = detect_semantic(typing.name, typing.plan, series, typing.sample, pii_type=pii.pii_type)
    if semantic in ("email", "phone") and not pii.is_pii:
        pii = type(pii)(True, semantic, "values", pii.hit_rate)
    stats = profile_column(series, typing.plan.pg_type, is_pii=pii.is_pii, invalid_count=typing.cast.invalid_count)
    return ColumnResult(
        ordinal=typing.position,
        name=typing.name,
        source_name=typing.source_name,
        data_type=typing.plan.pg_type,
        semantic_type=semantic,
        nullable=series.null_count() > 0 or series.len() == 0,
        is_pii=pii.is_pii,
        pii_type=pii.pii_type,
        pii_source=pii.source,
        invalid_count=typing.cast.invalid_count,
        stats=stats,
    )


def analyze(parsed: ParsedTable) -> AnalyzedTable:
    if parsed.frame.width == 0:
        raise IngestError("no_columns", "The file has no columns.")
    names = normalize_column_names(parsed.source_names)
    frame, typings = infer_table(parsed.frame, names, parsed.source_names, parsed.json_columns)
    columns = [_column_result(t, frame) for t in typings]
    profile = profile_dataset(frame, [(c.name, c.data_type) for c in columns])
    profile["invalid_cells"] = sum(c.invalid_count for c in columns)
    profile["parse"] = parsed.meta
    profile["pii_columns"] = [c.name for c in columns if c.is_pii]
    return AnalyzedTable(frame=frame, columns=columns, profile=profile)
