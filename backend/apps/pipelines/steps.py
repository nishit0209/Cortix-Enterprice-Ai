"""Transform steps (pure Polars) with static schema inference.

Each step has a strict pydantic config model (unknown keys rejected), an `infer(schema, cfg, ctx)` that checks
column references / types and returns the output schema, and an `apply(frame, cfg, ctx)` that executes it.
Filters are STRUCTURED conditions and `derive` uses the whitelisted expression tree in `expressions.py`:
no user-supplied code, SQL or eval anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import expressions
from . import types as T

ColName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
NewName = Annotated[str, StringConstraints(min_length=1, max_length=63, strip_whitespace=True)]
Scalar = str | int | float | bool | None


class StepConfigError(ValueError):
    """A config problem found while inferring a step's schema (reported per node / field)."""

    def __init__(self, message: str, *, field: str = "", code: str = "invalid"):
        super().__init__(message)
        self.field = field
        self.code = code


class StepRuntimeError(RuntimeError):
    def __init__(self, message: str, code: str = "step_failed", *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class StepContext:
    """IO the steps may use (injected so steps stay pure and testable)."""

    load_dataset: Callable[[UUID, int | None], pl.DataFrame] | None = None   # (dataset_id, row_limit)
    dataset_schema: Callable[[UUID], T.Schema | None] | None = None
    row_limit: int | None = None                                             # preview: cap side inputs too
    inputs: list[dict[str, Any]] = field(default_factory=list)               # side inputs read (for lineage)


@dataclass
class StepResult:
    frame: pl.DataFrame
    rejected: int = 0


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require(schema: T.Schema, cols: list[str] | tuple[str, ...], fld: str) -> None:
    missing = [c for c in cols if c not in schema]
    if missing:
        raise StepConfigError(f"Unknown column(s): {', '.join(missing)}.", field=fld, code="unknown_column")


def _dupes(names: list[str], fld: str) -> None:
    seen: set[str] = set()
    for n in names:
        if n in seen:
            raise StepConfigError(f"Column {n!r} appears twice.", field=fld, code="duplicate_column")
        seen.add(n)


class Step:
    type: ClassVar[str]
    config_model: ClassVar[type[_Cfg]]
    label: ClassVar[str] = ""

    def parse(self, config: dict[str, Any]) -> _Cfg:
        return self.config_model.model_validate(config or {})

    def infer(self, schema: T.Schema, cfg: Any, ctx: StepContext) -> T.Schema:  # pragma: no cover - abstract
        raise NotImplementedError

    def apply(self, frame: pl.DataFrame, cfg: Any, ctx: StepContext) -> StepResult:  # pragma: no cover
        raise NotImplementedError


STEPS: dict[str, Step] = {}


def register(cls: type[Step]) -> type[Step]:
    STEPS[cls.type] = cls()
    return cls


# ---------------------------------------------------------------------------------------------------
# schema steps
# ---------------------------------------------------------------------------------------------------
class SelectCfg(_Cfg):
    columns: list[ColName] = Field(min_length=1, max_length=1000)


@register
class SelectColumns(Step):
    type = "select_columns"
    config_model = SelectCfg
    label = "Select columns"

    def infer(self, schema, cfg: SelectCfg, ctx):
        _require(schema, cfg.columns, "columns")
        _dupes(cfg.columns, "columns")
        return {c: schema[c] for c in cfg.columns}

    def apply(self, frame, cfg: SelectCfg, ctx):
        return StepResult(frame.select(cfg.columns))


class RenameCfg(_Cfg):
    mapping: dict[ColName, NewName] = Field(min_length=1, max_length=1000)


@register
class Rename(Step):
    type = "rename"
    config_model = RenameCfg
    label = "Rename columns"

    def infer(self, schema, cfg: RenameCfg, ctx):
        _require(schema, list(cfg.mapping), "mapping")
        out_names = [cfg.mapping.get(c, c) for c in schema]
        dup = {n for n in out_names if out_names.count(n) > 1}
        if dup:
            raise StepConfigError(f"Renaming would produce duplicate column(s): {', '.join(sorted(dup))}.",
                                  field="mapping", code="duplicate_column")
        return {cfg.mapping.get(c, c): t for c, t in schema.items()}

    def apply(self, frame, cfg: RenameCfg, ctx):
        return StepResult(frame.rename(dict(cfg.mapping)))


class CastCfg(_Cfg):
    columns: dict[ColName, str] = Field(min_length=1, max_length=1000)
    on_error: Literal["null", "reject", "fail"] = "null"
    formats: dict[ColName, Annotated[str, StringConstraints(min_length=2, max_length=64)]] = Field(default_factory=dict)


@register
class Cast(Step):
    type = "cast"
    config_model = CastCfg
    label = "Change types"

    def infer(self, schema, cfg: CastCfg, ctx):
        _require(schema, list(cfg.columns), "columns")
        out = dict(schema)
        for col, target in cfg.columns.items():
            norm = T.normalize_cast_type(target)
            if norm is None:
                raise StepConfigError(f"Unknown type {target!r} for {col}.", field=f"columns.{col}",
                                      code="unknown_type")
            out[col] = norm
        for col in cfg.formats:
            if col not in cfg.columns:
                raise StepConfigError(f"A format is given for {col}, which is not cast.", field=f"formats.{col}")
        return out

    def apply(self, frame, cfg: CastCfg, ctx):
        exprs = []
        for col, target in cfg.columns.items():
            norm = T.normalize_cast_type(target) or T.TEXT
            exprs.append(expressions.cast_expr(pl.col(col), norm, cfg.formats.get(col), source=frame.schema[col])
                         .alias(col))
        cast = frame.with_columns(exprs)
        if frame.height == 0:
            return StepResult(cast)
        # a row "fails" when a non-empty input value became null
        mask = pl.Series("m", [False] * frame.height, dtype=pl.Boolean)
        for col in cfg.columns:
            src = frame[col]
            present = src.is_not_null()
            if src.dtype == pl.String:
                present = present & (src.str.strip_chars() != "")
            mask = mask | (present & cast[col].is_null()).fill_null(False)
        bad = int(mask.sum())
        if bad and cfg.on_error == "fail":
            raise StepRuntimeError(f"{bad} value(s) could not be converted.", "cast_failed")
        if bad and cfg.on_error == "reject":
            return StepResult(cast.filter(~mask), rejected=bad)
        return StepResult(cast)


# ---------------------------------------------------------------------------------------------------
# filtering & cleaning
# ---------------------------------------------------------------------------------------------------
FilterOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "not_null", "contains",
                   "starts_with", "ends_with", "between"]


class Condition(_Cfg):
    column: ColName
    op: FilterOp
    value: Scalar | list[Scalar] = None
    case_sensitive: bool = True

    @model_validator(mode="after")
    def _shape(self):
        if self.op in ("is_null", "not_null"):
            if self.value not in (None, []):
                raise ValueError(f"{self.op} takes no value.")
        elif self.op in ("in", "not_in"):
            if not isinstance(self.value, list) or not (1 <= len(self.value) <= 1000):
                raise ValueError(f"{self.op} needs a list of 1..1000 values.")
        elif self.op == "between":
            if not isinstance(self.value, list) or len(self.value) != 2 or None in self.value:
                raise ValueError("between needs [low, high].")
        else:
            if isinstance(self.value, list) or self.value is None:
                raise ValueError(f"{self.op} needs a single non-null value.")
        if self.op in ("contains", "starts_with", "ends_with") and not isinstance(self.value, str):
            raise ValueError(f"{self.op} needs a text value.")
        return self


class FilterCfg(_Cfg):
    conditions: list[Condition] = Field(min_length=1, max_length=50)
    match: Literal["all", "any"] = "all"


def _lit(value: Any, dtype: pl.DataType) -> pl.Expr:
    coerced = T.coerce_literal(value, dtype)
    if coerced is None:
        return pl.lit(None)
    if dtype.is_numeric() and isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
        return pl.lit(coerced)
    try:
        return pl.lit(coerced, dtype=dtype)
    except (TypeError, ValueError, pl.exceptions.PolarsError):
        return pl.lit(coerced)


def condition_expr(c: Condition, dtype: pl.DataType) -> pl.Expr:
    col = pl.col(c.column)
    if c.op == "is_null":
        return col.is_null()
    if c.op == "not_null":
        return col.is_not_null()
    if c.op in ("contains", "starts_with", "ends_with"):
        text = col.cast(pl.String)
        needle = str(c.value)
        if not c.case_sensitive:
            text, needle = text.str.to_lowercase(), needle.lower()
        if c.op == "contains":
            return text.str.contains(needle, literal=True).fill_null(False)
        if c.op == "starts_with":
            return text.str.starts_with(needle).fill_null(False)
        return text.str.ends_with(needle).fill_null(False)
    if c.op in ("in", "not_in"):
        values = [T.coerce_literal(v, dtype) for v in c.value]  # type: ignore[union-attr]
        member = col.is_in(pl.Series(values, dtype=dtype if not dtype.is_numeric() else None, strict=False))
        return member.fill_null(False) if c.op == "in" else (~member).fill_null(False)
    if c.op == "between":
        lo, hi = c.value  # type: ignore[misc]
        return ((col >= _lit(lo, dtype)) & (col <= _lit(hi, dtype))).fill_null(False)
    value = c.value
    if dtype == pl.String and not c.case_sensitive and isinstance(value, str):
        col, value = col.str.to_lowercase(), value.lower()
    rhs = _lit(value, dtype)
    expr = {"eq": col == rhs, "ne": col != rhs, "gt": col > rhs, "gte": col >= rhs, "lt": col < rhs,
            "lte": col <= rhs}[c.op]
    return expr.fill_null(False)


def _check_condition_types(c: Condition, schema: T.Schema, fld: str) -> None:
    t = schema[c.column]
    if c.op in ("gt", "gte", "lt", "lte", "between") and t in (T.BOOLEAN, T.JSONB):
        raise StepConfigError(f"{c.op} cannot be used on a {t} column.", field=fld, code="type_error")
    values = c.value if isinstance(c.value, list) else [c.value]
    for v in values:
        if v is None:
            continue
        if t in T.NUMERIC_TYPES and (isinstance(v, bool) or not isinstance(v, (int, float))) \
                and c.op not in ("contains", "starts_with", "ends_with"):
            raise StepConfigError(f"{c.column} is numeric; compare it with numbers.", field=fld, code="type_error")
        if t in T.TEMPORAL_TYPES and c.op not in ("contains", "starts_with", "ends_with"):
            try:
                T.coerce_literal(v, pl.Date() if t == T.DATE else pl.Datetime("us", "UTC"))
            except (TypeError, ValueError):
                raise StepConfigError(f"{v!r} is not an ISO date/time.", field=fld, code="type_error") from None


@register
class Filter(Step):
    type = "filter"
    config_model = FilterCfg
    label = "Filter rows"

    def infer(self, schema, cfg: FilterCfg, ctx):
        for i, c in enumerate(cfg.conditions):
            _require(schema, [c.column], f"conditions.{i}.column")
            _check_condition_types(c, schema, f"conditions.{i}.value")
        return dict(schema)

    def apply(self, frame, cfg: FilterCfg, ctx):
        exprs = [condition_expr(c, frame.schema[c.column]) for c in cfg.conditions]
        combined = pl.all_horizontal(exprs) if cfg.match == "all" else pl.any_horizontal(exprs)
        return StepResult(frame.filter(combined))


class FillNullCfg(_Cfg):
    columns: list[ColName] = Field(min_length=1, max_length=1000)
    strategy: Literal["value", "zero", "mean", "median", "mode", "forward", "backward"] = "value"
    value: Scalar = None

    @model_validator(mode="after")
    def _value(self):
        if self.strategy == "value" and self.value is None:
            raise ValueError("strategy 'value' needs a non-null value.")
        if self.strategy != "value" and self.value is not None:
            raise ValueError("value is only used with strategy 'value'.")
        return self


@register
class FillNull(Step):
    type = "fill_null"
    config_model = FillNullCfg
    label = "Fill missing values"

    def infer(self, schema, cfg: FillNullCfg, ctx):
        _require(schema, cfg.columns, "columns")
        for c in cfg.columns:
            t = schema[c]
            if cfg.strategy in ("mean", "median", "zero") and not T.is_numeric(t):
                raise StepConfigError(f"{cfg.strategy} needs numeric columns; {c} is {t}.", field="columns",
                                      code="type_error")
            if cfg.strategy == "value" and t in T.NUMERIC_TYPES and (
                    isinstance(cfg.value, bool) or not isinstance(cfg.value, (int, float))):
                raise StepConfigError(f"{c} is numeric; the fill value must be a number.", field="value",
                                      code="type_error")
        out = dict(schema)
        if cfg.strategy == "mean":
            out.update({c: T.DOUBLE for c in cfg.columns})
        return out

    def apply(self, frame, cfg: FillNullCfg, ctx):
        exprs = []
        for c in cfg.columns:
            col = pl.col(c)
            dtype = frame.schema[c]
            match cfg.strategy:
                case "value":
                    exprs.append(col.fill_null(_lit(cfg.value, dtype)).alias(c))
                case "zero":
                    exprs.append(col.fill_null(0).alias(c))
                case "mean":
                    exprs.append(col.cast(pl.Float64).fill_null(col.cast(pl.Float64).mean()).alias(c))
                case "median":
                    exprs.append(col.fill_null(col.median().cast(dtype, strict=False)).alias(c))
                case "mode":
                    exprs.append(col.fill_null(col.drop_nulls().mode().sort().first()).alias(c))
                case "forward":
                    exprs.append(col.fill_null(strategy="forward").alias(c))
                case "backward":
                    exprs.append(col.fill_null(strategy="backward").alias(c))
        return StepResult(frame.with_columns(exprs))


class DropNullsCfg(_Cfg):
    columns: list[ColName] | None = Field(default=None, max_length=1000)
    how: Literal["any", "all"] = "any"


@register
class DropNulls(Step):
    type = "drop_nulls"
    config_model = DropNullsCfg
    label = "Drop rows with missing values"

    def infer(self, schema, cfg: DropNullsCfg, ctx):
        if cfg.columns:
            _require(schema, cfg.columns, "columns")
        return dict(schema)

    def apply(self, frame, cfg: DropNullsCfg, ctx):
        cols = cfg.columns or frame.columns
        if not cols:
            return StepResult(frame)
        nulls = [pl.col(c).is_null() for c in cols]
        drop = pl.any_horizontal(nulls) if cfg.how == "any" else pl.all_horizontal(nulls)
        return StepResult(frame.filter(~drop))


class DedupCfg(_Cfg):
    subset: list[ColName] | None = Field(default=None, max_length=1000)
    keep: Literal["first", "last"] = "first"
    order_by: ColName | None = None
    descending: bool = False


@register
class Deduplicate(Step):
    type = "deduplicate"
    config_model = DedupCfg
    label = "Remove duplicates"

    def infer(self, schema, cfg: DedupCfg, ctx):
        if cfg.subset:
            _require(schema, cfg.subset, "subset")
        if cfg.order_by:
            _require(schema, [cfg.order_by], "order_by")
        return dict(schema)

    def apply(self, frame, cfg: DedupCfg, ctx):
        if cfg.order_by:
            frame = frame.sort(cfg.order_by, descending=cfg.descending, nulls_last=True, maintain_order=True)
        return StepResult(frame.unique(subset=cfg.subset or None, keep=cfg.keep, maintain_order=True))


class TextColumnsCfg(_Cfg):
    columns: list[ColName] | None = Field(default=None, max_length=1000)


def _text_columns(schema: T.Schema, cols: list[str] | None, fld: str) -> list[str]:
    if not cols:
        return [c for c, t in schema.items() if t == T.TEXT]
    _require(schema, cols, fld)
    bad = [c for c in cols if not T.is_text(schema[c])]
    if bad:
        raise StepConfigError(f"Not text column(s): {', '.join(bad)}.", field=fld, code="type_error")
    return list(cols)


@register
class TrimWhitespace(Step):
    type = "trim_whitespace"
    config_model = TextColumnsCfg
    label = "Trim whitespace"

    def infer(self, schema, cfg: TextColumnsCfg, ctx):
        _text_columns(schema, cfg.columns, "columns")
        return dict(schema)

    def apply(self, frame, cfg: TextColumnsCfg, ctx):
        cols = cfg.columns or [c for c, d in frame.schema.items() if d == pl.String]
        return StepResult(frame.with_columns([pl.col(c).str.strip_chars().alias(c) for c in cols]))


class CaseCfg(_Cfg):
    columns: list[ColName] = Field(min_length=1, max_length=1000)
    case: Literal["lower", "upper", "title"]


@register
class ChangeCase(Step):
    type = "change_case"
    config_model = CaseCfg
    label = "Change text case"

    def infer(self, schema, cfg: CaseCfg, ctx):
        _text_columns(schema, cfg.columns, "columns")
        return dict(schema)

    def apply(self, frame, cfg: CaseCfg, ctx):
        fn = {"lower": lambda e: e.str.to_lowercase(), "upper": lambda e: e.str.to_uppercase(),
              "title": lambda e: e.str.to_titlecase()}[cfg.case]
        return StepResult(frame.with_columns([fn(pl.col(c)).alias(c) for c in cfg.columns]))


# ---------------------------------------------------------------------------------------------------
# reshape
# ---------------------------------------------------------------------------------------------------
class DeriveCfg(_Cfg):
    name: NewName
    expression: dict[str, Any]
    replace: bool = False


@register
class Derive(Step):
    type = "derive"
    config_model = DeriveCfg
    label = "Add calculated column"

    def infer(self, schema, cfg: DeriveCfg, ctx):
        if cfg.name in schema and not cfg.replace:
            raise StepConfigError(f"Column {cfg.name!r} already exists (set replace: true to overwrite).",
                                  field="name", code="duplicate_column")
        try:
            t = expressions.check(cfg.expression, schema)
        except expressions.ExpressionError as exc:
            raise StepConfigError(str(exc), field="expression", code=exc.code) from None
        out = dict(schema)
        out[cfg.name] = t if t != T.UNKNOWN else T.TEXT
        return out

    def apply(self, frame, cfg: DeriveCfg, ctx):
        expr = expressions.compile_expr(cfg.expression, dict(frame.schema))
        try:
            return StepResult(frame.with_columns(expr.alias(cfg.name)))
        except pl.exceptions.PolarsError as exc:
            raise StepRuntimeError(f"The expression could not be evaluated: {type(exc).__name__}.",
                                   "expression_failed") from None


class SortKey(_Cfg):
    column: ColName
    descending: bool = False


class SortCfg(_Cfg):
    by: list[SortKey] = Field(min_length=1, max_length=20)
    nulls_last: bool = True


@register
class Sort(Step):
    type = "sort"
    config_model = SortCfg
    label = "Sort rows"

    def infer(self, schema, cfg: SortCfg, ctx):
        _require(schema, [k.column for k in cfg.by], "by")
        return dict(schema)

    def apply(self, frame, cfg: SortCfg, ctx):
        return StepResult(frame.sort([k.column for k in cfg.by], descending=[k.descending for k in cfg.by],
                                     nulls_last=cfg.nulls_last, maintain_order=True))


AggFunc = Literal["count", "count_distinct", "sum", "mean", "median", "min", "max", "std", "first", "last"]


class Aggregation(_Cfg):
    func: AggFunc
    column: ColName | None = None
    alias: NewName | None = None

    @model_validator(mode="after")
    def _column(self):
        if self.column is None and self.func != "count":
            raise ValueError(f"{self.func} needs a column.")
        return self

    @property
    def out_name(self) -> str:
        if self.alias:
            return self.alias
        return f"{self.column}_{self.func}" if self.column else "row_count"


class AggregateCfg(_Cfg):
    group_by: list[ColName] = Field(default_factory=list, max_length=50)
    aggregations: list[Aggregation] = Field(min_length=1, max_length=100)


def _agg_expr(func: str, column: str | None) -> pl.Expr:
    if column is None:
        return pl.len().cast(pl.Int64)
    col = pl.col(column)
    match func:
        case "count":
            return col.count().cast(pl.Int64)
        case "count_distinct":
            return col.drop_nulls().n_unique().cast(pl.Int64)
        case "sum":
            return col.sum()
        case "mean":
            return col.mean()
        case "median":
            return col.median()
        case "min":
            return col.min()
        case "max":
            return col.max()
        case "std":
            return col.std()
        case "first":
            return col.first()
        case "last":
            return col.last()
    raise StepRuntimeError(f"Unknown aggregation {func!r}.", "invalid_aggregation")  # pragma: no cover


@register
class Aggregate(Step):
    type = "aggregate"
    config_model = AggregateCfg
    label = "Group and aggregate"

    def infer(self, schema, cfg: AggregateCfg, ctx):
        _require(schema, cfg.group_by, "group_by")
        _dupes(cfg.group_by, "group_by")
        out = {c: schema[c] for c in cfg.group_by}
        for i, a in enumerate(cfg.aggregations):
            fld = f"aggregations.{i}"
            if a.column is not None:
                _require(schema, [a.column], f"{fld}.column")
            t = schema.get(a.column, T.BIGINT) if a.column else T.BIGINT
            if a.func in ("sum", "mean", "median", "std") and not T.is_numeric(t):
                raise StepConfigError(f"{a.func} needs a numeric column; {a.column} is {t}.", field=fld,
                                      code="type_error")
            if a.func in ("count", "count_distinct"):
                rt = T.BIGINT
            elif a.func in ("mean", "median", "std"):
                rt = T.DOUBLE
            elif a.func == "sum":
                rt = T.BIGINT if t == T.BIGINT else T.DOUBLE
            else:
                rt = t
            if a.out_name in out:
                raise StepConfigError(f"Output column {a.out_name!r} appears twice; set an alias.", field=fld,
                                      code="duplicate_column")
            out[a.out_name] = rt
        return out

    def apply(self, frame, cfg: AggregateCfg, ctx):
        aggs = [_agg_expr(a.func, a.column).alias(a.out_name) for a in cfg.aggregations]
        if cfg.group_by:
            out = frame.group_by(cfg.group_by, maintain_order=True).agg(aggs)
        else:
            out = frame.select(aggs)
        return StepResult(out)


class JoinKey(_Cfg):
    left: ColName
    right: ColName


class JoinCfg(_Cfg):
    dataset_id: UUID
    how: Literal["inner", "left"] = "inner"
    on: list[JoinKey] = Field(min_length=1, max_length=10)
    columns: list[ColName] | None = Field(default=None, max_length=1000)
    suffix: Annotated[str, StringConstraints(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_]+$")] = "_right"


@register
class Join(Step):
    type = "join"
    config_model = JoinCfg
    label = "Join another dataset"

    def _right_schema(self, cfg: JoinCfg, ctx: StepContext) -> T.Schema | None:
        if ctx.dataset_schema is None:
            return None
        return ctx.dataset_schema(cfg.dataset_id)

    def infer(self, schema, cfg: JoinCfg, ctx):
        _require(schema, [k.left for k in cfg.on], "on")
        right = self._right_schema(cfg, ctx)
        if right is None:
            if ctx.dataset_schema is not None:
                raise StepConfigError("The dataset to join does not exist or has no ready version.",
                                      field="dataset_id", code="dataset_not_found")
            right = {}
            return dict(schema)  # schema unknown (pure validation without IO)
        _require(right, [k.right for k in cfg.on], "on")
        if cfg.columns:
            _require(right, cfg.columns, "columns")
        for k in cfg.on:
            lt, rt = schema[k.left], right[k.right]
            if lt != rt and not (lt in T.NUMERIC_TYPES and rt in T.NUMERIC_TYPES) and T.UNKNOWN not in (lt, rt):
                raise StepConfigError(f"Join keys {k.left} ({lt}) and {k.right} ({rt}) have different types.",
                                      field="on", code="type_error")
        right_keys = {k.right for k in cfg.on}
        out = dict(schema)
        for c in (cfg.columns or list(right)):
            if c in right_keys:
                continue
            name = c if c not in out else f"{c}{cfg.suffix}"
            if name in out:
                raise StepConfigError(f"Column {name!r} would appear twice; choose another suffix.",
                                      field="suffix", code="duplicate_column")
            out[name] = right[c]
        return out

    def apply(self, frame, cfg: JoinCfg, ctx):
        if ctx.load_dataset is None:
            raise StepRuntimeError("Joins need dataset access.", "join_unavailable")
        right = ctx.load_dataset(cfg.dataset_id, None)
        ctx.inputs.append({"kind": "dataset", "dataset_id": str(cfg.dataset_id), "role": "join"})
        right_keys = [k.right for k in cfg.on]
        keep = [c for c in (cfg.columns or right.columns) if c not in right_keys]
        right = right.select([*dict.fromkeys(right_keys), *keep])
        # align key dtypes (e.g. Int64 vs Float64) so the join is well defined
        casts = []
        for k in cfg.on:
            ld, rd = frame.schema[k.left], right.schema[k.right]
            if ld != rd:
                if ld.is_numeric() and rd.is_numeric():
                    casts.append((k.left, k.right, pl.Float64))
                else:
                    casts.append((k.left, k.right, pl.String))
        for lcol, rcol, dtype in casts:
            frame = frame.with_columns(pl.col(lcol).cast(dtype))
            right = right.with_columns(pl.col(rcol).cast(dtype))
        rename = {}
        for c in keep:
            if c in frame.columns:
                rename[c] = f"{c}{cfg.suffix}"
        right = right.rename(rename)
        out = frame.join(right, left_on=[k.left for k in cfg.on], right_on=right_keys, how=cfg.how,
                         coalesce=True, nulls_equal=False, maintain_order="left")
        return StepResult(out)


TRANSFORM_TYPES = tuple(STEPS)
