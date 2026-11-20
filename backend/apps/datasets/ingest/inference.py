"""Type inference (the ladder) and casting of all-text columns into PostgreSQL-typed Polars columns.

Ladder: boolean -> bigint -> numeric/double precision -> date -> timestamptz -> text.
A type wins only if >= 99.5% of the non-null sample values parse. Values that fail to parse in the chosen
type become NULL and are counted as `invalid_count` (a validity issue in the quality report).

Performance (large files, e.g. 1M+ rows): each ladder rung is first tried on a small head of the sample; when that
head alone already has more failures than the whole sample may have, the rung is skipped (an exact bound, not a
heuristic). Full-column casts first parse with the shortest PREFIX of the format family that contains every format
seen in the sample, then re-try only the rows that failed with the whole family. Parsing is a first-match coalesce in
family order, so a prefix match is the same first match as in the whole family: results equal a single pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import polars as pl

PARSE_THRESHOLD = 0.995
SAMPLE_HEAD = 100_000
SAMPLE_RANDOM = 50_000

NULL_TOKENS = ["", "na", "n/a", "null", "none", "nan", "#n/a", "nil", "nat", "#null!"]
BOOL_TRUE = ["true", "t", "yes", "y"]
BOOL_FALSE = ["false", "f", "no", "n"]

INT_RE = r"^[+-]?(?:0|[1-9][0-9]{0,17})$"
# currency symbols are written as escapes (U+20B9 rupee, U+20AC euro, U+00A3 pound) so the regex is encoding-proof
CURRENCY_SYMBOL_RE = r"(?i)(?:₹|\brs\.?|\binr\b|\busd\b|\beur\b|\bgbp\b|\$|€|£)"
CURRENCY_DETECT_RE = r"(?i)(?:₹|\brs\.?\s*\d|\binr\b|\busd\b|\beur\b|\bgbp\b|\$|€|£)"
GROUPED_RE = r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3})(?:\.\d+)?$"
PERCENT_RE = r"%\s*$"
NUMERIC_SHAPE_RE = r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$"

ISO_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")
DMY_DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d-%b-%y", "%d/%m/%y",
)
MDY_DATE_FORMATS = ("%m-%d-%Y", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y")
TZ_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S%.f%#z", "%Y-%m-%d %H:%M%#z")
ISO_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")
DMY_DATETIME_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p")
MDY_DATETIME_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m-%d-%Y %H:%M:%S")

PG_TYPES = ("boolean", "bigint", "numeric", "double precision", "date", "timestamptz", "text", "jsonb")


@dataclass(frozen=True)
class TypePlan:
    pg_type: str
    formats: tuple[str, ...] = ()
    currency_ratio: float = 0.0
    percent_ratio: float = 0.0
    # shortest prefix of `formats` holding every format that parsed a sample value: tried first on the full column
    primary_formats: tuple[str, ...] = field(default=(), compare=False)


@dataclass
class CastResult:
    series: pl.Series
    invalid_count: int
    null_count: int


@dataclass
class ColumnTyping:
    position: int
    name: str
    source_name: str
    plan: TypePlan
    cast: CastResult
    sample: list[str] = field(default_factory=list)  # cleaned non-null raw values (for semantic/PII detection)


# ---------------------------------------------------------------------------------------------------
# expression helpers
# ---------------------------------------------------------------------------------------------------
def _eval(series: pl.Series, expr: pl.Expr) -> pl.Series:
    return pl.DataFrame({"v": series}).select(expr.alias("v")).to_series()


def clean_expr(e: pl.Expr) -> pl.Expr:
    stripped = e.str.strip_chars()
    return pl.when(stripped.str.to_lowercase().is_in(NULL_TOKENS)).then(pl.lit(None, dtype=pl.String)).otherwise(stripped)


def clean_series(series: pl.Series) -> pl.Series:
    return _eval(series.cast(pl.String), clean_expr(pl.col("v")))


def bool_expr(e: pl.Expr) -> pl.Expr:
    low = e.str.to_lowercase()
    return (
        pl.when(low.is_in(BOOL_TRUE)).then(pl.lit(True))
        .when(low.is_in(BOOL_FALSE)).then(pl.lit(False))
        .otherwise(pl.lit(None, dtype=pl.Boolean))
    )


def int_expr(e: pl.Expr) -> pl.Expr:
    return (
        pl.when(e.str.contains(INT_RE))
        .then(e.str.strip_prefix("+").cast(pl.Int64, strict=False))
        .otherwise(pl.lit(None, dtype=pl.Int64))
    )


def numeric_expr(e: pl.Expr) -> pl.Expr:
    x = e.str.replace(r"^\((.*)\)$", "-${1}")             # accounting negatives: (1,234.50)
    x = x.str.replace_all(CURRENCY_SYMBOL_RE, "")
    x = x.str.replace_all(r"\s+", "")
    x = x.str.replace(r"%$", "")
    x = x.str.replace(r"^-\+|^\+-", "-")
    x = pl.when(x.str.contains(GROUPED_RE)).then(x.str.replace_all(",", "", literal=True)).otherwise(x)
    x = pl.when(x.str.contains(NUMERIC_SHAPE_RE)).then(x).otherwise(pl.lit(None, dtype=pl.String))
    f = x.cast(pl.Float64, strict=False)
    return pl.when(f.is_finite()).then(f).otherwise(pl.lit(None, dtype=pl.Float64))


def _normalize_datetime_text(e: pl.Expr) -> pl.Expr:
    x = e.str.replace(r"^(\d{4}-\d{1,2}-\d{1,2})[Tt]", "${1} ")
    x = x.str.replace(r"(?i)\s*(?:z|utc|gmt)$", "+00:00")
    return x


def date_expr(e: pl.Expr, formats: Sequence[str]) -> pl.Expr:
    return pl.coalesce([e.str.strptime(pl.Date, f, strict=False) for f in formats])


def datetime_expr(e: pl.Expr, formats: Sequence[str]) -> pl.Expr:
    """Formats with %z parse as UTC instants; naive formats and date-only formats are taken as UTC."""
    x = _normalize_datetime_text(e)
    parts = []
    for f in formats:
        if f.startswith("@date:"):
            parts.append(
                x.str.strptime(pl.Date, f[6:], strict=False).cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
            )
        elif "%z" in f or "%#z" in f:
            parts.append(x.str.strptime(pl.Datetime("us", "UTC"), f, strict=False))
        else:
            parts.append(x.str.strptime(pl.Datetime("us"), f, strict=False).dt.replace_time_zone("UTC"))
    return pl.coalesce(parts)


def parse_expr(e: pl.Expr, plan: TypePlan) -> pl.Expr:
    t = plan.pg_type
    if t == "boolean":
        return bool_expr(e)
    if t == "bigint":
        if plan.currency_ratio or plan.percent_ratio:
            return numeric_expr(e).cast(pl.Int64, strict=False)
        return pl.coalesce([int_expr(e), _integral_float_expr(e)])
    if t in ("numeric", "double precision"):
        return numeric_expr(e)
    if t == "date":
        return date_expr(e, plan.formats)
    if t == "timestamptz":
        return datetime_expr(e, plan.formats)
    return e


def _integral_float_expr(e: pl.Expr) -> pl.Expr:
    """'1,234' or '12.0' in a bigint column (grouping / Excel floats)."""
    f = numeric_expr(e)
    return pl.when(f.is_not_null() & (f == f.round(0)) & (f.abs() < 9e15)).then(f.cast(pl.Int64)).otherwise(
        pl.lit(None, dtype=pl.Int64)
    )


# ---------------------------------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------------------------------
def sample_values(cleaned: pl.Series, *, head: int = SAMPLE_HEAD, random: int = SAMPLE_RANDOM, seed: int = 7) -> pl.Series:
    nn = cleaned.drop_nulls()
    if nn.len() <= head + random:
        return nn
    rest = nn.slice(head)
    return pl.concat([nn.head(head), rest.sample(n=random, seed=seed)])


def _ratio(parsed: pl.Series, total: int) -> float:
    return (parsed.len() - parsed.null_count()) / total if total else 0.0


PROBE_ROWS = 2000


def _cannot_reach(sample: pl.Series, expr: pl.Expr, threshold: float) -> bool:
    """Exact early exit: True when the first PROBE_ROWS values alone fail more often than the whole sample may."""
    total = sample.len()
    if total <= PROBE_ROWS:
        return False
    probe = _eval(sample.head(PROBE_ROWS), expr)
    return probe.null_count() > (1 - threshold) * total


def _ratio_at_least(sample: pl.Series, expr: pl.Expr, threshold: float) -> bool:
    if _cannot_reach(sample, expr, threshold):
        return False
    return _ratio(_eval(sample, expr), sample.len()) >= threshold


def _best_family(sample: pl.Series, families: list[tuple[str, tuple[str, ...]]], parse,
                 threshold: float = 0.0) -> tuple[float, tuple[str, ...]]:
    best_ratio, best_formats = 0.0, ()
    for _, formats in families:
        expr = parse(pl.col("v"), formats)
        if threshold and _cannot_reach(sample, expr, threshold):
            continue
        ratio = _ratio(_eval(sample, expr), sample.len())
        if ratio > best_ratio + 1e-12:
            best_ratio, best_formats = ratio, formats
    return best_ratio, best_formats


def _hit_formats(sample: pl.Series, formats: tuple[str, ...], parse) -> tuple[str, ...]:
    """The shortest prefix of `formats` that contains every format parsing at least one sample value.

    A prefix (not just the hits) keeps the first-match order exact: a value that an earlier, unseen format would also
    parse still gets that earlier format's reading in the first pass.
    """
    last = -1
    for i, f in enumerate(formats):
        if _eval(sample, parse(pl.col("v"), (f,))).null_count() < sample.len():
            last = i
    return formats[: last + 1]


def infer_plan(cleaned_sample: pl.Series, *, is_json: bool = False, threshold: float = PARSE_THRESHOLD) -> TypePlan:
    """Choose the physical type for a column from its cleaned, non-null text sample."""
    if is_json:
        return TypePlan("jsonb")
    total = cleaned_sample.len()
    if total == 0:
        return TypePlan("text")
    if _ratio_at_least(cleaned_sample, bool_expr(pl.col("v")), threshold):
        return TypePlan("boolean")
    # leading-zero codes ("007", "0987654321") and >15-digit numbers (card/account numbers, which would lose
    # precision as floats) are identifiers, never numbers
    leading_zero = _eval(cleaned_sample, pl.col("v").str.contains(r"^0\d|^[+-]?\d{16,}$")).sum()
    if leading_zero / total <= 1 - threshold:
        if _ratio_at_least(cleaned_sample, int_expr(pl.col("v")), threshold):
            return TypePlan("bigint")
        num_possible = not _cannot_reach(cleaned_sample, numeric_expr(pl.col("v")), threshold)
        num = _eval(cleaned_sample, numeric_expr(pl.col("v"))) if num_possible else None
        if num is not None and _ratio(num, total) >= threshold:
            currency = float(_eval(cleaned_sample, pl.col("v").str.contains(CURRENCY_DETECT_RE)).mean() or 0.0)
            percent = float(_eval(cleaned_sample, pl.col("v").str.contains(PERCENT_RE)).mean() or 0.0)
            has_point = bool(_eval(cleaned_sample, pl.col("v").str.contains(r"\.\d|[eE]")).any())
            nn = num.drop_nulls()
            integral = nn.len() > 0 and bool((nn == nn.round(0)).all()) and float(nn.abs().max()) < 9e15
            if currency >= 0.5:
                return TypePlan("numeric", currency_ratio=currency, percent_ratio=percent)
            if integral and not has_point and percent < 0.5:
                return TypePlan("bigint", currency_ratio=currency, percent_ratio=percent)
            return TypePlan("double precision", currency_ratio=currency, percent_ratio=percent)
    date_ratio, date_formats = _best_family(
        cleaned_sample,
        [("dmy", ISO_DATE_FORMATS + DMY_DATE_FORMATS), ("mdy", ISO_DATE_FORMATS + MDY_DATE_FORMATS)],
        date_expr,
        threshold,
    )
    if date_ratio >= threshold:
        return TypePlan("date", formats=date_formats,
                        primary_formats=_hit_formats(cleaned_sample, date_formats, date_expr))
    date_tags = lambda fmts: tuple(f"@date:{f}" for f in fmts)  # noqa: E731
    ts_ratio, ts_formats = _best_family(
        cleaned_sample,
        [
            ("dmy", TZ_DATETIME_FORMATS + ISO_DATETIME_FORMATS + DMY_DATETIME_FORMATS
             + date_tags(ISO_DATE_FORMATS + DMY_DATE_FORMATS)),
            ("mdy", TZ_DATETIME_FORMATS + ISO_DATETIME_FORMATS + MDY_DATETIME_FORMATS
             + date_tags(ISO_DATE_FORMATS + MDY_DATE_FORMATS)),
        ],
        datetime_expr,
        threshold,
    )
    if ts_ratio >= threshold:
        return TypePlan("timestamptz", formats=ts_formats,
                        primary_formats=_hit_formats(cleaned_sample, ts_formats, datetime_expr))
    return TypePlan("text")


def cast_column(raw: pl.Series, plan: TypePlan) -> CastResult:
    """Cast an all-text column according to the plan; count values that failed to parse."""
    raw = raw.cast(pl.String)
    if plan.pg_type in ("text", "jsonb"):
        # text keeps its value; only empty / whitespace-only cells become NULL
        typed = _eval(
            raw, pl.when(pl.col("v").str.strip_chars() == "").then(pl.lit(None, dtype=pl.String)).otherwise(pl.col("v"))
        )
        return CastResult(series=typed, invalid_count=0, null_count=typed.null_count())
    cleaned = clean_series(raw)
    typed = _parse_column(cleaned, plan)
    invalid = int((cleaned.is_not_null() & typed.is_null()).sum())
    return CastResult(series=typed, invalid_count=invalid, null_count=typed.null_count())


def _retry_failed(cleaned: pl.Series, typed: pl.Series, fallback: pl.Expr) -> pl.Series:
    """Re-parse only the rows the first pass could not parse with `fallback`, and merge the results."""
    failed = cleaned.is_not_null() & typed.is_null()
    if not failed.any():
        return typed
    idx = failed.arg_true()
    retried = _eval(cleaned.gather(idx), fallback).cast(typed.dtype)
    return typed.scatter(idx, retried)


def _parse_column(cleaned: pl.Series, plan: TypePlan) -> pl.Series:
    """`parse_expr(plan)` over a full column, staged for speed with identical results (see module docstring)."""
    v = pl.col("v")
    t = plan.pg_type
    if t in ("date", "timestamptz") and plan.primary_formats and plan.primary_formats != plan.formats:
        parse = date_expr if t == "date" else datetime_expr
        typed = _eval(cleaned, parse(v, plan.primary_formats))
        return _retry_failed(cleaned, typed, parse(v, plan.formats))
    if t == "bigint" and not (plan.currency_ratio or plan.percent_ratio):
        typed = _eval(cleaned, int_expr(v))
        return _retry_failed(cleaned, typed, _integral_float_expr(v))
    return _eval(cleaned, parse_expr(v, plan))


def parse_as_type(raw: pl.Series, pg_type: str) -> pl.Series:
    """Parse text values as `pg_type` with the default format families (used by `type` quality rules)."""
    cleaned = clean_series(raw.cast(pl.String))
    if pg_type == "date":
        plan = TypePlan("date", formats=ISO_DATE_FORMATS + DMY_DATE_FORMATS + MDY_DATE_FORMATS)
    elif pg_type == "timestamptz":
        plan = TypePlan("timestamptz", formats=TZ_DATETIME_FORMATS + ISO_DATETIME_FORMATS + DMY_DATETIME_FORMATS)
    else:
        plan = TypePlan(pg_type)
    return _eval(cleaned, parse_expr(pl.col("v"), plan))


def infer_and_cast(raw: pl.Series, *, is_json: bool = False) -> tuple[TypePlan, CastResult, list[str]]:
    cleaned = clean_series(raw)
    sample = sample_values(cleaned)
    plan = infer_plan(sample, is_json=is_json)
    result = cast_column(raw, plan)
    small_sample = sample.head(5000).to_list()
    return plan, result, small_sample


def infer_table(frame: pl.DataFrame, names: Sequence[str], source_names: Sequence[str],
                json_positions: set[int] | None = None) -> tuple[pl.DataFrame, list[ColumnTyping]]:
    """Type every positional column `c<i>` of an all-text frame; returns the typed frame with final names."""
    json_positions = json_positions or set()
    typed_cols: list[pl.Series] = []
    out: list[ColumnTyping] = []
    for i, name in enumerate(names):
        raw = frame[f"c{i}"]
        plan, result, sample = infer_and_cast(raw, is_json=i in json_positions)
        typed_cols.append(result.series.alias(name))
        out.append(ColumnTyping(position=i, name=name, source_name=source_names[i], plan=plan, cast=result, sample=sample))
    typed = pl.DataFrame(typed_cols) if typed_cols else pl.DataFrame()
    return typed, out


def schema_hash(columns: Sequence[tuple[str, str]]) -> str:
    import hashlib

    payload = "|".join(f"{n}:{t}" for n, t in columns)
    return hashlib.sha256(payload.encode()).hexdigest()
