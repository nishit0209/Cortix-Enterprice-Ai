"""Governed data access for the ML workspace (CONTRACT W4 "Cross-app Python services").

Every frame the workspace trains on or scores comes through `governed_frame`:
  1. `apps.governance.services.effective_policy(user, dataset, privilege="query")` (404 when the user has no access);
  2. only ALLOWED and UNMASKED columns are loaded; masked/denied columns are listed as `excluded_by_policy`;
  3. the user's row policies (RowPredicate) are applied to the frame, exactly like the SQL guard would;
  4. the frame is read from the dataset version through `apps.datasets.services`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pandas as pd
import polars as pl

from .engine.frames import to_pandas
from .exceptions import PolicyExcludedColumns


@dataclass
class GovernedFrame:
    frame: pl.DataFrame
    dataset_id: UUID
    dataset_name: str
    classification: str
    version_id: UUID
    version_no: int
    all_columns: list[str]
    allowed_columns: list[str]
    excluded_by_policy: list[dict] = field(default_factory=list)
    row_filtered: bool = False
    source_rows: int = 0

    def pandas(self) -> pd.DataFrame:
        return to_pandas(self.frame)


def _datasets():
    from apps.datasets import services as ds

    return ds


def _policy_columns(user_id, dataset_id, tables, columns_by_table) -> tuple[list[str], list[dict], Any]:
    from apps.governance.services import effective_policy

    policy = effective_policy(user_id, dataset_id, tables, columns_by_table, privilege="query")
    logical = next(iter(tables))
    every = list(columns_by_table[logical])
    allowed = set(policy.allowed_columns.get(logical, frozenset()))
    masked = {col for (tbl, col) in policy.column_masks if tbl == logical}
    usable = [c for c in every if c in allowed and c not in masked]
    excluded = [{"column": c, "reason": "masked" if c in masked else "denied"} for c in every if c not in usable]
    return usable, excluded, policy


_OPS = {
    "=": lambda c, v: c == v, "!=": lambda c, v: c != v, "<": lambda c, v: c < v, "<=": lambda c, v: c <= v,
    ">": lambda c, v: c > v, ">=": lambda c, v: c >= v,
}


def _literal(value: Any, dtype) -> pl.Expr:
    lit = pl.lit(value)
    try:
        return lit.cast(dtype, strict=False)
    except Exception:  # noqa: BLE001 - fall back to the raw literal
        return lit


def apply_row_predicates(frame: pl.DataFrame, conditions: Sequence[tuple]) -> pl.DataFrame:
    """AND of (column, op, value) conditions with SQL NULL semantics (a NULL comparison never matches)."""
    expr = pl.lit(True)
    for col, op, value in conditions:
        if col not in frame.columns:
            return frame.head(0)  # a policy on a column we cannot evaluate: fail closed
        c = pl.col(col)
        dtype = frame.schema[col]
        if op == "is_null":
            cond = c.is_null()
        elif op == "not_null":
            cond = c.is_not_null()
        elif op in ("in", "not_in"):
            values = list(value or ())
            if not values:
                cond = c.is_not_null() if op == "not_in" else pl.lit(False)
            else:
                series = pl.Series(values)
                with contextlib.suppress(Exception):  # incomparable literal: keep the raw values
                    series = series.cast(dtype, strict=False)
                cond = c.is_in(series.to_list()) if op == "in" else ~c.is_in(series.to_list())
                cond = cond & c.is_not_null()
        elif op in _OPS:
            cond = _OPS[op](c, _literal(value, dtype))
        else:
            return frame.head(0)  # unknown operator: fail closed
        expr = expr & cond.fill_null(False)
    return frame.filter(expr)


def governed_frame(user_id, dataset_id, *, columns: Sequence[str] | None = None, version_no: int | None = None,
                   version_id=None, limit: int | None = None, require: Sequence[str] = ()) -> GovernedFrame:
    """The user's policy-filtered view of a dataset version as a pandas frame.

    `columns`: the subset wanted (default: every usable column). `require`: columns that MUST be usable, otherwise
    PolicyExcludedColumns (403) lists them."""
    ds = _datasets()
    dataset = ds.get_dataset(dataset_id)
    if version_id is not None:
        from apps.datasets.models import DatasetVersion

        version = DatasetVersion.objects.filter(dataset_id=dataset.id, id=version_id, status="ready").first()
        if version is None:
            from apps.datasets.exceptions import DatasetNotReady

            raise DatasetNotReady()
    else:
        version = ds.require_ready_version(dataset, version_no)
    tables, columns_by_table = ds.version_schema(dataset, version)
    usable, excluded, policy = _policy_columns(user_id, dataset.id, tables, columns_by_table)
    missing = [c for c in require if c not in usable]
    if missing:
        raise PolicyExcludedColumns(extra={"excluded_by_policy": missing})
    wanted = [c for c in (columns if columns is not None else usable) if c in usable]
    logical = next(iter(tables))
    predicate = policy.row_predicates.get(logical)
    conditions = list(predicate.conditions) if predicate else []
    extra = [c for c, _, _ in conditions if c not in wanted and c in columns_by_table[logical]]
    load_cols = list(dict.fromkeys([*wanted, *extra]))
    frame = ds.load_version_frame(version, load_cols, limit=None if conditions else limit) if load_cols \
        else pl.DataFrame()
    source_rows = frame.height
    if conditions:
        frame = apply_row_predicates(frame, conditions)
        if limit is not None:
            frame = frame.head(limit)
    if extra:
        frame = frame.drop(extra)
    return GovernedFrame(frame=frame, dataset_id=dataset.id, dataset_name=dataset.name,
                         classification=dataset.classification, version_id=version.id, version_no=version.version,
                         all_columns=list(columns_by_table[logical]), allowed_columns=usable,
                         excluded_by_policy=excluded, row_filtered=bool(conditions), source_rows=source_rows)


def allowed_columns(user_id, dataset_id) -> tuple[list[str], list[dict]]:
    """(usable columns, excluded_by_policy) for the user on the dataset's current version (no data read)."""
    ds = _datasets()
    dataset = ds.get_dataset(dataset_id)
    version = ds.require_ready_version(dataset)
    tables, columns_by_table = ds.version_schema(dataset, version)
    usable, excluded, _ = _policy_columns(user_id, dataset.id, tables, columns_by_table)
    return usable, excluded
