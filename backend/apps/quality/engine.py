"""Rule validation + evaluation with Polars, rule suggestions from a profile, and quality scoring.

Pure functions (no ORM) so pipelines' `quality.gate` step (wave 2) can reuse them.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import polars as pl

from .expressions import ExpressionError, compile_expression

NUMERIC = ("bigint", "double precision", "numeric")
TEMPORAL = ("date", "timestamptz")
PG_TYPES = ("boolean", "bigint", "double precision", "numeric", "date", "timestamptz", "text", "jsonb")
SEVERITY_WEIGHTS = {"error": 3, "warn": 1, "info": 0}
VALIDITY_RULES = ("range", "regex", "allowed_values", "type", "referential", "expression")
SAMPLE_FAILURES = 20
MAX_ALLOWED_VALUES = 1000
MAX_REGEX_LENGTH = 500
RANGE_MARGIN = 0.10
ALLOWED_VALUES_MAX_DISTINCT = 20


class RuleValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class RuleSpec:
    rule_type: str
    column: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    severity: str = "warn"
    is_blocking: bool = False
    id: str | None = None


@dataclass
class RuleOutcome:
    passed: bool
    evaluated_count: int
    failed_count: int
    sample_failures: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def pass_rate(self) -> float:
        if self.error:
            return 0.0
        if self.evaluated_count <= 0:
            return 1.0 if self.passed else 0.0
        return max(0.0, 1.0 - self.failed_count / self.evaluated_count)


@dataclass
class EvalContext:
    column_types: Mapping[str, str]
    pii_columns: set[str] = field(default_factory=set)
    # (dataset_id, column) -> values of that column in the referenced dataset's current version
    reference_values: Callable[[UUID, str], pl.Series] | None = None
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    type_parser: Callable[[pl.Series, str], pl.Series] | None = None


def mask_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if len(text) <= 2:
        return "**"
    return "*" * min(8, len(text) - 2) + text[-2:]


def _parse_bound(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type in NUMERIC:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("must be a number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return number
    if pg_type == "date":
        return dt.date.fromisoformat(str(value)[:10])
    if pg_type == "timestamptz":
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    raise ValueError("range rules need a numeric or date column")


# ---------------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------------
def validate_rule(spec: RuleSpec, column_types: Mapping[str, str]) -> dict:
    """Validate and normalize params; raises RuleValidationError({field: message})."""
    t, col, params = spec.rule_type, spec.column, dict(spec.params or {})
    errors: dict[str, str] = {}
    needs_column = t not in ("row_count", "expression") and not (t == "unique" and params.get("columns"))
    if needs_column:
        if not col:
            errors["column"] = "This rule type needs a column."
        elif col not in column_types:
            errors["column"] = f"Unknown column '{col}'."
    if spec.severity not in SEVERITY_WEIGHTS:
        errors["severity"] = "Must be info, warn or error."
    if errors:
        raise RuleValidationError(errors)
    pg = column_types.get(col or "", "")
    out: dict[str, Any] = {}
    if t in ("not_null",):
        pass
    elif t == "unique":
        cols = params.get("columns")
        if cols:
            if not isinstance(cols, list) or not all(isinstance(c, str) and c in column_types for c in cols):
                errors["params.columns"] = "Must be a list of existing column names."
            else:
                out["columns"] = cols
    elif t == "range":
        try:
            lo = _parse_bound(params.get("min"), pg)
            hi = _parse_bound(params.get("max"), pg)
        except ValueError as exc:
            errors["params"] = str(exc) or "Invalid bound."
        else:
            if lo is None and hi is None:
                errors["params"] = "Give min and/or max."
            elif lo is not None and hi is not None and lo > hi:
                errors["params"] = "min must be <= max."
            out.update(min=params.get("min"), max=params.get("max"), inclusive=bool(params.get("inclusive", True)))
    elif t == "regex":
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_REGEX_LENGTH:
            errors["params.pattern"] = f"A pattern of 1..{MAX_REGEX_LENGTH} characters is required."
        else:
            try:
                pl.Series(["probe"]).str.contains(f"^(?:{pattern})$")
            except pl.exceptions.PolarsError:
                errors["params.pattern"] = "The regular expression is invalid."
            out["pattern"] = pattern
    elif t == "allowed_values":
        values = params.get("values")
        if not isinstance(values, list) or not values or len(values) > MAX_ALLOWED_VALUES:
            errors["params.values"] = f"Give 1..{MAX_ALLOWED_VALUES} values."
        elif not all(isinstance(v, (str, int, float, bool)) for v in values):
            errors["params.values"] = "Values must be scalars."
        else:
            out["values"] = values
    elif t == "type":
        data_type = params.get("data_type")
        if data_type not in PG_TYPES:
            errors["params.data_type"] = f"Must be one of {', '.join(PG_TYPES)}."
        else:
            out["data_type"] = data_type
    elif t == "freshness":
        if pg not in TEMPORAL:
            errors["column"] = "Freshness rules need a date or timestamp column."
        try:
            hours = float(params.get("max_age_hours"))
            if not math.isfinite(hours) or hours <= 0:
                raise ValueError
            out["max_age_hours"] = hours
        except (TypeError, ValueError):
            errors["params.max_age_hours"] = "Must be a positive number of hours."
    elif t == "row_count":
        lo, hi = params.get("min"), params.get("max")
        if lo is None and hi is None:
            errors["params"] = "Give min and/or max."
        for key, v in (("min", lo), ("max", hi)):
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                errors[f"params.{key}"] = "Must be a non-negative integer."
        if not errors and lo is not None and hi is not None and lo > hi:
            errors["params"] = "min must be <= max."
        out.update(min=lo, max=hi)
    elif t == "referential":
        try:
            out["dataset_id"] = str(UUID(str(params.get("dataset_id"))))
        except ValueError:
            errors["params.dataset_id"] = "Must be a dataset UUID."
        ref_col = params.get("column")
        if not isinstance(ref_col, str) or not ref_col:
            errors["params.column"] = "The referenced column is required."
        else:
            out["column"] = ref_col
    elif t == "expression":
        try:
            compile_expression(str(params.get("expression") or ""), column_types.keys())
            out["expression"] = str(params["expression"])
        except ExpressionError as exc:
            errors["params.expression"] = str(exc)
    else:
        errors["rule_type"] = f"Unknown rule type '{t}'."
    if errors:
        raise RuleValidationError(errors)
    return out


# ---------------------------------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------------------------------
def _samples(frame: pl.DataFrame, mask: pl.Series, column: str | None, ctx: EvalContext) -> list[dict]:
    idx = frame.with_row_index("__row").filter(mask)
    if idx.height == 0:
        return []
    rows = idx.head(SAMPLE_FAILURES)
    out = []
    for row in rows.iter_rows(named=True):
        item = {"row": int(row["__row"]) + 1}
        if column is not None and column in row:
            value = row[column]
            if isinstance(value, (dt.date, dt.datetime)):
                value = value.isoformat()
            item["value"] = mask_value(value) if column in ctx.pii_columns else value
        out.append(item)
    return out


def _outcome(frame, fail_mask: pl.Series, evaluated: int, column, ctx) -> RuleOutcome:
    fail_mask = fail_mask.fill_null(False)
    failed = int(fail_mask.sum())
    return RuleOutcome(
        passed=failed == 0,
        evaluated_count=int(evaluated),
        failed_count=failed,
        sample_failures=_samples(frame, fail_mask, column, ctx) if failed else [],
    )


def evaluate_rule(frame: pl.DataFrame, spec: RuleSpec, ctx: EvalContext) -> RuleOutcome:
    try:
        return _evaluate(frame, spec, ctx)
    except (RuleValidationError, ExpressionError, ValueError, pl.exceptions.PolarsError, KeyError) as exc:
        return RuleOutcome(passed=False, evaluated_count=0, failed_count=0, error=str(exc)[:300] or type(exc).__name__)


def _evaluate(frame: pl.DataFrame, spec: RuleSpec, ctx: EvalContext) -> RuleOutcome:
    t, col = spec.rule_type, spec.column
    params = validate_rule(spec, ctx.column_types)
    n = frame.height
    if t == "row_count":
        lo, hi = params.get("min"), params.get("max")
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        return RuleOutcome(passed=ok, evaluated_count=1, failed_count=0 if ok else 1)
    if t == "expression":
        expr, _ = compile_expression(params["expression"], ctx.column_types.keys())
        result = frame.select(expr.alias("ok")).to_series()
        return _outcome(frame, result == False, n, None, ctx)  # noqa: E712 - NULL counts as passing (SQL CHECK)
    if t == "unique" and params.get("columns"):
        cols = params["columns"]
        sub = frame.select(cols)
        complete = sub.select(pl.all_horizontal(pl.all().is_not_null())).to_series()
        dup = sub.is_duplicated() & complete
        return _outcome(frame, dup, int(complete.sum()), cols[0], ctx)
    s = frame[col]
    pg = ctx.column_types[col]
    nonnull = s.is_not_null()
    evaluated = int(nonnull.sum())
    if t == "not_null":
        return _outcome(frame, s.is_null(), n, col, ctx)
    if t == "unique":
        return _outcome(frame, s.is_duplicated() & nonnull, evaluated, col, ctx)
    if t == "range":
        lo, hi = _parse_bound(params.get("min"), pg), _parse_bound(params.get("max"), pg)
        inclusive = params.get("inclusive", True)
        values = s.cast(pl.Float64) if pg in NUMERIC else s
        bad = pl.Series([False] * n)
        if lo is not None:
            bad = bad | ((values < lo) if inclusive else (values <= lo)).fill_null(False)
        if hi is not None:
            bad = bad | ((values > hi) if inclusive else (values >= hi)).fill_null(False)
        return _outcome(frame, bad, evaluated, col, ctx)
    if t == "regex":
        text = s.cast(pl.String)
        bad = nonnull & ~text.str.contains(f"^(?:{params['pattern']})$").fill_null(False)
        return _outcome(frame, bad, evaluated, col, ctx)
    if t == "allowed_values":
        values = params["values"]
        if pg in NUMERIC:
            allowed = [float(v) for v in values if not isinstance(v, bool) and _is_number(v)]
            ok = s.cast(pl.Float64).is_in(allowed)
        elif pg == "boolean":
            allowed_b = [_to_bool(v) for v in values]
            ok = s.is_in([b for b in allowed_b if b is not None])
        else:
            ok = s.cast(pl.String).is_in([str(v) for v in values])
        return _outcome(frame, nonnull & ~ok.fill_null(False), evaluated, col, ctx)
    if t == "type":
        target = params["data_type"]
        if pg == target or target in ("text",) or (target == "double precision" and pg in NUMERIC) or (
            target == "numeric" and pg in NUMERIC
        ):
            return RuleOutcome(passed=True, evaluated_count=evaluated, failed_count=0)
        if ctx.type_parser is None:
            raise ValueError("type parsing is unavailable")
        parsed = ctx.type_parser(s.cast(pl.String), target)
        return _outcome(frame, nonnull & parsed.is_null(), evaluated, col, ctx)
    if t == "freshness":
        latest = s.drop_nulls().max()
        if latest is None:
            return RuleOutcome(passed=False, evaluated_count=1, failed_count=1)
        if isinstance(latest, dt.datetime):
            latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=dt.UTC)
        else:
            latest_dt = dt.datetime.combine(latest, dt.time.max, tzinfo=dt.UTC)
        age_h = (ctx.now() - latest_dt).total_seconds() / 3600
        ok = age_h <= params["max_age_hours"]
        return RuleOutcome(passed=ok, evaluated_count=1, failed_count=0 if ok else 1,
                           sample_failures=[] if ok else [{"latest": latest_dt.isoformat(), "age_hours": round(age_h, 2)}])
    if t == "referential":
        if ctx.reference_values is None:
            raise ValueError("referential lookups are unavailable")
        ref = ctx.reference_values(UUID(params["dataset_id"]), params["column"])
        ref_values = ref.drop_nulls().cast(pl.String).unique()
        ok = s.cast(pl.String).is_in(ref_values.to_list())
        return _outcome(frame, nonnull & ~ok.fill_null(False), evaluated, col, ctx)
    raise ValueError(f"unsupported rule type {t}")


def _is_number(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    text = str(v).strip().lower()
    if text in ("true", "t", "yes", "y", "1"):
        return True
    if text in ("false", "f", "no", "n", "0"):
        return False
    return None


# ---------------------------------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------------------------------
def suggest_rules(columns: Sequence[Mapping[str, Any]], dataset_profile: Mapping[str, Any]) -> list[RuleSpec]:
    """Local rule suggestions from a profile.

    columns: [{name, data_type, semantic_type, is_pii, stats}] ; dataset_profile: {row_count, candidate_keys}
    """
    rows = int(dataset_profile.get("row_count") or 0)
    if rows == 0:
        return []
    keys = set(dataset_profile.get("candidate_keys") or [])
    out: list[RuleSpec] = []
    for c in columns:
        name, pg, stats = c["name"], c["data_type"], c.get("stats") or {}
        if pg == "jsonb":
            continue
        if stats.get("null_count", 1) == 0:
            out.append(RuleSpec("not_null", name, {}, "warn"))
        if name in keys and (c.get("semantic_type") in ("id", "email") or re.search(r"(^id$|_id$|_key$|_code$|_no$)", name)):
            out.append(RuleSpec("unique", name, {}, "error"))
        if pg in NUMERIC and stats.get("p1") is not None and stats.get("p99") is not None:
            p1, p99 = float(stats["p1"]), float(stats["p99"])
            span = (p99 - p1) or abs(p99) or 1.0
            lo, hi = p1 - RANGE_MARGIN * span, p99 + RANGE_MARGIN * span
            if stats.get("min") is not None and float(stats["min"]) >= 0 and lo < 0:
                lo = 0.0
            if pg == "bigint":
                lo, hi = math.floor(lo), math.ceil(hi)
            out.append(RuleSpec("range", name, {"min": round(lo, 6), "max": round(hi, 6), "inclusive": True}, "info"))
        distinct = int(stats.get("distinct") or 0)
        top = stats.get("top_values") or []
        if (
            not c.get("is_pii")
            and pg in ("text", "bigint", "boolean")
            and 0 < distinct <= ALLOWED_VALUES_MAX_DISTINCT
            and distinct < rows
            and c.get("semantic_type") in ("category", "boolean", "geo_country", "geo_city", "number", "text")
            and len(top) >= min(distinct, 10)
            and distinct <= 10
        ):
            values = [t["value"] for t in top if t.get("value") is not None]
            if values and len(values) == distinct:
                out.append(RuleSpec("allowed_values", name, {"values": values}, "warn"))
    return out


# ---------------------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------------------
def _pct(x: float) -> float:
    return round(max(0.0, min(100.0, 100.0 * x)), 2)


def compute_dimensions(
    frame: pl.DataFrame,
    column_types: Mapping[str, str],
    outcomes: Sequence[tuple[RuleSpec, RuleOutcome]],
    duplicates: Mapping[str, Any],
    *,
    invalid_cells: int = 0,
) -> dict[str, float]:
    rows, cols = frame.height, max(1, frame.width)
    cells = rows * cols
    nulls = int(sum(frame[c].null_count() for c in frame.columns)) if rows else 0
    completeness = 1.0 - (nulls / cells if cells else 0.0)

    uniq_parts = [1.0 - (duplicates.get("exact_rows", 0) / rows if rows else 0.0)]
    for key in duplicates.get("key_columns", []):
        nonnull = key.get("non_null") or 0
        if nonnull:
            uniq_parts.append(1.0 - key.get("duplicate_rows", 0) / nonnull)
    uniq_parts += [o.pass_rate for s, o in outcomes if s.rule_type == "unique"]
    uniqueness = sum(uniq_parts) / len(uniq_parts)

    non_null_cells = max(1, cells - nulls + invalid_cells)
    valid_parts = [1.0 - invalid_cells / non_null_cells] + [o.pass_rate for s, o in outcomes if s.rule_type in VALIDITY_RULES]
    validity = sum(valid_parts) / len(valid_parts)

    checked = duplicates.get("consistency_checked_rows", 0)
    inconsistent = duplicates.get("inconsistent_rows", 0)
    consistency = 1.0 - (inconsistent / checked if checked else 0.0)

    dims = {
        "completeness": _pct(completeness),
        "uniqueness": _pct(uniqueness),
        "validity": _pct(validity),
        "consistency": _pct(consistency),
    }
    fresh = [o.pass_rate for s, o in outcomes if s.rule_type == "freshness"]
    if fresh:
        dims["freshness"] = _pct(sum(fresh) / len(fresh))
    return dims


def compute_score(dimensions: Mapping[str, float], outcomes: Sequence[tuple[RuleSpec, RuleOutcome]]) -> float:
    """0-100: half the severity-weighted rule pass rate (error x3, warn x1, info x0), half the dimension mean."""
    core = [dimensions[k] for k in ("completeness", "uniqueness", "validity", "consistency") if k in dimensions]
    dim_score = sum(core) / len(core) if core else 100.0
    weights = [(SEVERITY_WEIGHTS.get(s.severity, 0), o.pass_rate) for s, o in outcomes]
    total_w = sum(w for w, _ in weights)
    if total_w <= 0:
        return round(dim_score, 2)
    rule_score = 100.0 * sum(w * r for w, r in weights) / total_w
    return round(0.5 * rule_score + 0.5 * dim_score, 2)
