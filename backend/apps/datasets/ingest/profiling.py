"""Column and dataset profiling with Polars (local; nothing leaves the server).

Per column: null %, distinct (approximate above 1M rows), min/max/mean/stddev/p5/p50/p95, text length stats,
top-10 values (suppressed for PII), 20-bin histogram.
Dataset: row count, duplicate rows, candidate keys (uniqueness >= 99.9%), correlated numeric pairs (|r| > 0.9).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from typing import Any

import polars as pl

HIST_BINS = 20
TOP_N = 10
APPROX_DISTINCT_ABOVE = 1_000_000
CANDIDATE_KEY_UNIQUENESS = 0.999
CORRELATION_THRESHOLD = 0.9
MAX_CORRELATION_COLUMNS = 25

NUMERIC_TYPES = ("bigint", "double precision", "numeric")
TEMPORAL_TYPES = ("date", "timestamptz")


def _num(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _distinct(series: pl.Series) -> int:
    nn = series.drop_nulls()
    if nn.len() > APPROX_DISTINCT_ABOVE:
        return int(pl.DataFrame({"v": nn}).select(pl.col("v").approx_n_unique()).item())
    return int(nn.n_unique())


def histogram(values: pl.Series, bins: int = HIST_BINS) -> list[dict]:
    """Equal-width histogram over finite numeric values: [{start, end, count}]."""
    nn = values.drop_nulls().cast(pl.Float64)
    nn = nn.filter(nn.is_finite())
    if nn.len() == 0:
        return []
    lo, hi = float(nn.min()), float(nn.max())
    if lo == hi:
        return [{"start": lo, "end": hi, "count": int(nn.len())}]
    width = (hi - lo) / bins
    idx = ((nn - lo) / width).floor().cast(pl.Int64).clip(0, bins - 1)
    vc = pl.DataFrame({"b": idx}).group_by("b").len()
    counts = dict(zip(vc["b"].to_list(), vc["len"].to_list(), strict=True))
    return [
        {"start": round(lo + i * width, 6), "end": round(lo + (i + 1) * width, 6), "count": int(counts.get(i, 0))}
        for i in range(bins)
    ]


def _temporal_histogram(values: pl.Series, pg_type: str) -> list[dict]:
    nn = values.drop_nulls()
    if nn.len() == 0:
        return []
    if pg_type == "date":
        epoch = nn.cast(pl.Int32).cast(pl.Float64)  # days since epoch
        to_val = lambda x: (dt.date(1970, 1, 1) + dt.timedelta(days=round(x))).isoformat()  # noqa: E731
    else:
        epoch = nn.dt.epoch("s").cast(pl.Float64)
        to_val = lambda x: dt.datetime.fromtimestamp(x, tz=dt.UTC).isoformat()  # noqa: E731
    hist = histogram(epoch)
    return [{"start": to_val(h["start"]), "end": to_val(h["end"]), "count": h["count"]} for h in hist]


def top_values(series: pl.Series, n: int = TOP_N) -> list[dict]:
    nn = series.drop_nulls()
    if nn.len() == 0:
        return []
    vc = pl.DataFrame({"value": nn}).group_by("value").len().sort(["len", "value"], descending=[True, False]).head(n)
    return [{"value": _num(v), "count": int(c)} for v, c in zip(vc["value"].to_list(), vc["len"].to_list(), strict=True)]


def profile_column(series: pl.Series, pg_type: str, *, is_pii: bool = False, invalid_count: int = 0) -> dict:
    total = series.len()
    nulls = series.null_count()
    non_null = total - nulls
    stats: dict[str, Any] = {
        "count": total,
        "null_count": nulls,
        "null_pct": round(100.0 * nulls / total, 2) if total else 0.0,
        "distinct": _distinct(series) if non_null else 0,
        "invalid_count": int(invalid_count),
        "min": None, "max": None, "mean": None, "stddev": None, "p5": None, "p50": None, "p95": None,
        "top_values": None if is_pii else [],
        "histogram": [],
    }
    nn = series.drop_nulls()
    if pg_type in NUMERIC_TYPES and non_null:
        f = nn.cast(pl.Float64)
        stats.update(
            min=_num(nn.min()), max=_num(nn.max()), mean=_num(f.mean()), stddev=_num(f.std()) if non_null > 1 else None,
            p5=_num(f.quantile(0.05, "linear")), p50=_num(f.quantile(0.5, "linear")),
            p95=_num(f.quantile(0.95, "linear")),
            p1=_num(f.quantile(0.01, "linear")), p99=_num(f.quantile(0.99, "linear")),
            histogram=histogram(f),
        )
    elif pg_type in TEMPORAL_TYPES and non_null:
        stats.update(min=_num(nn.min()), max=_num(nn.max()), p50=_num(nn.sort()[non_null // 2]),
                     histogram=_temporal_histogram(nn, pg_type))
    elif pg_type in ("text", "jsonb") and non_null:
        lengths = nn.str.len_chars()
        stats.update(min_length=int(lengths.min()), max_length=int(lengths.max()),
                     avg_length=_num(float(lengths.mean())))
        if not is_pii and pg_type == "text":
            stats.update(min=nn.min(), max=nn.max())
    elif pg_type == "boolean" and non_null:
        trues = int(nn.sum())
        stats.update(true_count=trues, false_count=int(non_null - trues),
                     mean=_num(trues / non_null))
    if not is_pii and non_null:
        stats["top_values"] = top_values(nn)
    return stats


def duplicate_row_count(frame: pl.DataFrame) -> int:
    if frame.height == 0 or frame.width == 0:
        return 0
    return int(frame.height - frame.n_unique())


def candidate_keys(frame: pl.DataFrame, columns: Sequence[tuple[str, str]]) -> list[str]:
    keys = []
    n = frame.height
    if n == 0:
        return keys
    for name, pg_type in columns:
        if pg_type in ("jsonb", "boolean", "double precision"):
            continue
        s = frame[name]
        if s.null_count() > 0:
            continue
        if s.n_unique() / n >= CANDIDATE_KEY_UNIQUENESS:
            keys.append(name)
    return keys


def correlated_pairs(frame: pl.DataFrame, columns: Sequence[tuple[str, str]]) -> list[dict]:
    nums = [n for n, t in columns if t in NUMERIC_TYPES][:MAX_CORRELATION_COLUMNS]
    if len(nums) < 2 or frame.height < 3:
        return []
    pairs = []
    sub = frame.select([pl.col(n).cast(pl.Float64) for n in nums])
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            r = sub.select(pl.corr(a, b)).item()
            if r is not None and math.isfinite(r) and abs(r) > CORRELATION_THRESHOLD:
                pairs.append({"a": a, "b": b, "r": round(float(r), 4)})
    return pairs


def profile_dataset(frame: pl.DataFrame, columns: Sequence[tuple[str, str]]) -> dict:
    return {
        "row_count": frame.height,
        "column_count": frame.width,
        "duplicate_rows": duplicate_row_count(frame),
        "candidate_keys": candidate_keys(frame, columns),
        "correlated_pairs": correlated_pairs(frame, columns),
    }


def schema_drift(previous: Sequence[tuple[str, str]], current: Sequence[tuple[str, str]]) -> dict:
    prev, cur = dict(previous), dict(current)
    return {
        "added": [n for n in cur if n not in prev],
        "removed": [n for n in prev if n not in cur],
        "retyped": [{"column": n, "from": prev[n], "to": cur[n]} for n in cur if n in prev and prev[n] != cur[n]],
    }
