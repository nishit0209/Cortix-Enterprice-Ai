"""Local insights + templated narration (0 Gemini calls by default) — AI_ARCHITECTURE.md §4 / §6.7.

compute_insights(columns, rows, *, complete=None) -> [{kind: summary|period_change|trend|growth|top_n|share|anomaly,
                                                      text, data}]
narrate_template(question, insights) -> plain-English narrative with the real numbers
narrate_with_gemini(...) -> streamed narration, ONLY when the policy allows VALUES for the asset
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Sequence
from datetime import date, datetime

import numpy as np
import pandas as pd

ANOMALY_Z = 3.0
MIN_ANOMALY_POINTS = 8
MIN_TREND_POINTS = 4
TREND_R2 = 0.5
PERIOD_NAMES = {"period", "month", "week", "day", "quarter", "year", "date"}


# ------------------------------------------------------------------------------------------- formatting
def fmt_num(v: float | int | None) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    v = float(v)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:,.2f}M"
    if a >= 1e4:
        return f"{v:,.0f}"
    if v.is_integer():
        return f"{int(v):,}"
    return f"{v:,.2f}"


def fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _label(v) -> str:
    if isinstance(v, (pd.Timestamp, datetime)):
        ts = pd.Timestamp(v)
        return ts.strftime("%Y-%m-%d") if (ts.hour, ts.minute, ts.second) != (0, 0, 0) or ts.day != 1 else ts.strftime("%b %Y")
    if isinstance(v, date):
        return v.strftime("%b %Y") if v.day == 1 else v.isoformat()
    return str(v)


def _human(col: str) -> str:
    return re.sub(r"_+", " ", str(col)).strip()


# ------------------------------------------------------------------------------------------- frame
def _column_names(columns: Sequence) -> list[str]:
    names = []
    for c in columns:
        if isinstance(c, str):
            names.append(c)
        elif isinstance(c, dict):
            names.append(str(c.get("name")))
        else:
            names.append(str(getattr(c, "name", c)))
    return names


def _masked(columns: Sequence) -> set[str]:
    out = set()
    for c in columns:
        masked = c.get("masked") if isinstance(c, dict) else getattr(c, "masked", False)
        if masked:
            out.add(c.get("name") if isinstance(c, dict) else getattr(c, "name", ""))
    return out


def _frame(columns: Sequence, rows: Sequence[Sequence]) -> pd.DataFrame:
    names = _column_names(columns)
    df = pd.DataFrame([list(r) for r in rows], columns=names)
    return df.drop(columns=[c for c in _masked(columns) if c in df.columns])


def _classify(df: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    numeric, temporal, categorical = [], [], []
    for col in df.columns:
        s = df[col].dropna()
        if s.empty:
            continue
        if pd.api.types.is_bool_dtype(s):
            categorical.append(col)
        elif pd.api.types.is_numeric_dtype(s):
            numeric.append(col)
        elif pd.api.types.is_datetime64_any_dtype(s) or all(isinstance(v, (date, datetime)) for v in s.head(50)):
            temporal.append(col)
        else:
            converted = pd.to_numeric(s, errors="coerce")
            if converted.notna().all() and not s.map(lambda v: isinstance(v, str)).all():
                numeric.append(col)
            elif s.map(lambda v: isinstance(v, str)).all() and _looks_like_dates(s):
                temporal.append(col)
            else:
                categorical.append(col)
    return numeric, temporal, categorical


def _looks_like_dates(s: pd.Series) -> bool:
    sample = s.head(20).astype(str)
    if not sample.str.match(r"^\d{4}-\d{2}(-\d{2})?").all():
        return False
    return pd.to_datetime(sample, errors="coerce").notna().all()


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


# ------------------------------------------------------------------------------------------- naming
ID_RE = re.compile(r"(?:^id$|_id$|^id_)", re.I)
DERIVED_RE = re.compile(r"(?:^|_)(?:pct|percent|percentage|share|ratio|rate|growth|change)(?:_|$)", re.I)
NON_ADDITIVE_RE = re.compile(
    r"^(?:avg|average|mean|median|min|max|minimum|maximum|stddev)_|_per_|"
    r"(?:^|_)(?:pct|percent|percentage|share|ratio|rate|growth|change|avg|average|price|discount)(?:_|$)", re.I)
_AGG_PREFIX = (("avg_", "average "), ("average_", "average "), ("mean_", "average "), ("median_", "median "),
               ("max_", "maximum "), ("min_", "minimum "), ("total_", "total "), ("sum_", "total "))


def human_measure(col: str) -> str:
    """'total_revenue' -> 'total revenue', 'distinct_order_id' -> 'number of orders', 'row_count' -> 'number of
    rows', 'revenue_per_order' -> 'revenue per order'."""
    raw = str(col).strip()
    low = raw.lower()
    if low == "row_count":
        return "number of rows"
    if low.startswith("distinct_"):
        base = _human(re.sub(r"_id$", "", raw[len("distinct_"):]))
        return f"number of {base if base.endswith('s') else base + 's'}"
    for prefix, word in _AGG_PREFIX:
        if low.startswith(prefix) and len(raw) > len(prefix):
            return word + _human(raw[len(prefix):])
    return _human(raw)


def _base_name(col: str) -> str:
    """Name used after 'the total ...': 'total_revenue' -> 'revenue'."""
    name = human_measure(col)
    return name[len("total "):] if name.startswith("total ") else name


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _period_phrase(raw) -> str:
    """'current_period' -> 'the current period', 'last_month' -> 'last month'."""
    text = _human(raw).lower()
    if text in ("current period", "previous period", "prior period"):
        return f"the {text}"
    return text


def is_additive(col: str) -> bool:
    """Shares of a total only make sense for sums and counts (not averages, prices, rates or ratios)."""
    return not NON_ADDITIVE_RE.search(str(col))


def _pick_measure(numeric: list[str]) -> str:
    plain = [c for c in numeric if not ID_RE.search(str(c)) and not DERIVED_RE.search(str(c))]
    if plain:
        return plain[0]
    not_id = [c for c in numeric if not ID_RE.search(str(c))]
    return (not_id or numeric)[0]


# ------------------------------------------------------------------------------------------- time grain
_GRAIN_DAYS = (("day", 0.5, 1.5), ("week", 6, 8), ("month", 27, 32), ("quarter", 85, 95), ("year", 360, 370))


def detect_grain(stamps: Sequence[pd.Timestamp]) -> str | None:
    if len(stamps) < 2:
        if len(stamps) == 1:
            ts = pd.Timestamp(stamps[0])
            if (ts.hour, ts.minute, ts.second) == (0, 0, 0) and ts.day == 1:
                return "month"
        return None
    deltas = np.diff(np.array([pd.Timestamp(s).value for s in stamps], dtype=np.int64)) / 86_400e9
    med = float(np.median(deltas))
    for name, lo, hi in _GRAIN_DAYS:
        if lo <= med <= hi:
            return name
    return None


def _next_bucket(ts: pd.Timestamp, grain: str) -> pd.Timestamp:
    if grain == "day":
        return ts + pd.Timedelta(days=1)
    if grain == "week":
        return ts + pd.Timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[grain]
    return ts + pd.DateOffset(months=months)


def bucket_label(value, grain: str | None) -> str:
    if grain is None:
        return _label(value)
    ts = pd.Timestamp(value)
    if grain == "day":
        return ts.strftime("%Y-%m-%d")
    if grain == "week":
        return f"week of {ts.strftime('%Y-%m-%d')}"
    if grain == "month":
        return ts.strftime("%b %Y")
    if grain == "quarter":
        return f"Q{(ts.month - 1) // 3 + 1} {ts.year}"
    return str(ts.year)


# ------------------------------------------------------------------------------------------- insights
def compute_insights(columns: Sequence, rows: Sequence[Sequence], *, max_insights: int = 6,
                     today: date | None = None, complete: bool | None = None) -> list[dict]:
    """Deterministic insights from a (<= 1,000 row) result set.

    kinds: summary (one value), period_change (two periods / latest vs previous bucket), trend (slope + overall %
    change), growth (largest period-over-period moves), top_n (top contributors with their shares), share (the
    leader's share; additive measures only), anomaly (robust z-score >= 3).
    `today` marks an in-progress last time bucket (default: today's date).
    `complete` says whether `rows` are ALL the groups (no LIMIT cut, not truncated): only then is the sum of the
    rows called "the total". Unknown (None) or False -> shares are stated against the rows shown, unless a share
    column computed in SQL over every group (e.g. the builder's `share_pct`) is present.
    """
    if not rows or not columns:
        return []
    df = _frame(columns, rows)
    if df.empty:
        return []
    numeric, temporal, categorical = _classify(df)
    out: list[dict] = []
    if not numeric:
        return out
    measure = _pick_measure(numeric)
    period_col = next((c for c in categorical if str(c).lower() in PERIOD_NAMES), None)

    # compare-periods shape: rows labelled this_X / last_X (or current_period / previous_period)
    if period_col is not None and len(df) == 2:
        pc = _period_compare(df, period_col, measure)
        if pc:
            out.append(pc)
            return out[:max_insights]

    if len(df) == 1:
        if len(numeric) >= 2:
            pc = _wide_compare(df, numeric)
            if pc:
                return [pc]
        if not temporal and not categorical:
            value = float(_num(df[measure]).iloc[0])
            return [{"kind": "summary", "text": f"{_cap(human_measure(measure))} is {fmt_num(value)}.",
                     "data": {"measure": measure, "value": value}}]

    if temporal:
        growth_col = next((c for c in numeric if re.search(r"growth|change", str(c), re.I) and c != measure), None)
        out.extend(_time_insights(df, temporal[0], measure, today=today or date.today(), growth_col=growth_col))
    elif categorical:
        out.extend(_category_insights(df, categorical[0], measure, complete=complete,
                                      share_col=_share_column(numeric, measure)))
        out.extend(_anomalies(df, measure, label_col=categorical[0]))
    return out[:max_insights]


def _period_compare(df: pd.DataFrame, period_col: str, measure: str) -> dict | None:
    labels = df[period_col].astype(str).str.lower().tolist()
    cur_idx = next((i for i, lab in enumerate(labels) if lab.startswith(("this", "current"))), None)
    prev_idx = next((i for i, lab in enumerate(labels) if lab.startswith(("last", "previous", "prior"))), None)
    if cur_idx is None or prev_idx is None:
        return None
    cur, prev = float(_num(df[measure]).iloc[cur_idx]), float(_num(df[measure]).iloc[prev_idx])
    return _change_insight(measure, _period_phrase(df[period_col].iloc[cur_idx]), cur,
                           _period_phrase(df[period_col].iloc[prev_idx]), prev)


def _wide_compare(df: pd.DataFrame, numeric: list[str]) -> dict | None:
    cur_col = next((c for c in numeric if str(c).lower().startswith(("this", "current"))), None)
    prev_col = next((c for c in numeric if str(c).lower().startswith(("last", "previous", "prior"))), None)
    if not cur_col or not prev_col:
        return None
    return _change_insight("value", _period_phrase(cur_col), float(_num(df[cur_col]).iloc[0]),
                           _period_phrase(prev_col), float(_num(df[prev_col]).iloc[0]))


def _pct_change(cur: float, prev: float) -> float | None:
    if prev in (0, 0.0) or math.isnan(prev) or math.isnan(cur):
        return None
    return (cur - prev) / abs(prev)


def _change_insight(measure: str, cur_label: str, cur: float, prev_label: str, prev: float) -> dict:
    delta = cur - prev
    pct = _pct_change(cur, prev)
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    name = "the value" if measure == "value" else human_measure(measure)
    if pct is None:
        text = f"{_cap(name)} was {fmt_num(cur)} in {cur_label} versus {fmt_num(prev)} in {prev_label}."
    else:
        verb = {"up": "rose", "down": "fell", "flat": "was unchanged"}[direction]
        change = f" {fmt_pct(abs(pct))}" if direction != "flat" else ""
        text = (f"{_cap(name)} {verb}{change} from {fmt_num(prev)} in {prev_label} "
                f"to {fmt_num(cur)} in {cur_label}.")
    return {"kind": "period_change", "text": text,
            "data": {"measure": measure, "current": cur, "previous": prev, "current_label": cur_label,
                     "previous_label": prev_label, "change": delta, "change_pct": pct, "direction": direction}}


SHARE_COL_RE = re.compile(r"(?:^|_)(?:share|(?:pct|percent|percentage)_of_total)(?:_|$)", re.I)
_PERCENT_NAME_RE = re.compile(r"pct|percent", re.I)


def _share_column(numeric: list[str], measure: str) -> str | None:
    """A column holding each row's share of the WHOLE result (computed in SQL, e.g. `SUM(x) OVER ()`)."""
    return next((c for c in numeric if c != measure and SHARE_COL_RE.search(str(c))), None)


def _plural(word: str, n: int) -> str:
    if n == 1:
        return word
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word if word.endswith("s") else word + "s"


def _category_insights(df: pd.DataFrame, dim: str, measure: str, *, complete: bool | None = None,
                       share_col: str | None = None) -> list[dict]:
    values = _num(df[measure])
    labels = df[dim].map(lambda v: "(blank)" if v is None or v == "" else str(v))
    frame = pd.DataFrame({"dim": labels, "v": values}).dropna()
    if frame.empty:
        return []
    additive = is_additive(measure)
    agg = frame.groupby("dim", sort=False)["v"]
    grouped = (agg.sum() if additive else agg.mean()).sort_values(ascending=False, kind="stable")
    shown_total = float(grouped.sum())
    shares: pd.Series | None = None
    basis = None
    if additive and len(grouped) >= 1 and bool((grouped >= 0).all()):
        col_shares = _column_shares(df, labels, share_col) if share_col else None
        if col_shares is not None:
            shares, basis = col_shares.reindex(grouped.index), "total"  # computed in SQL over every group
        elif len(grouped) >= 2 and shown_total > 0:
            shares, basis = grouped / shown_total, ("total" if complete is True else "shown")
    if shares is not None and shares.isna().any():
        shares, basis = None, None
    out: list[dict] = []
    n = min(3, len(grouped))
    top = grouped.head(n)
    items = [{"label": k, "value": float(v), **({"share": float(shares[k])} if shares is not None else {})}
             for k, v in top.items()]
    name = human_measure(measure)
    pct_in_listing = basis == "total"
    if len(grouped) >= 2:
        listing = ", ".join(f"{i['label']} ({fmt_num(i['value'])}" + (f", {fmt_pct(i['share'])})" if pct_in_listing
                                                                       else ")") for i in items)
        text = f"Top {_human(dim)} by {name}: {listing}."
    else:
        text = f"{items[0]['label']} has {name} of {fmt_num(items[0]['value'])}."
    data = {"dimension": dim, "measure": measure, "items": items}
    if basis:
        data["share_basis"] = basis
    out.append({"kind": "top_n", "text": text, "data": data})
    if shares is not None and (len(grouped) >= 2 or basis == "total"):
        leader = grouped.index[0]
        share = float(shares.iloc[0])
        topn_share = float(shares.head(n).sum())
        base = _base_name(measure)
        total = shown_total if complete is True else None  # the rows ARE every group -> their sum is the total
        if basis == "shown":
            groups = _plural(_human(dim), len(grouped))
            text = (f"{leader} accounts for {fmt_pct(share)} of the combined {base} of the {len(grouped)} {groups} "
                    f"shown ({fmt_num(shown_total)}).")
        elif total is None:  # shares from the SQL share column; the overall total itself is not in the rows
            text = f"{leader} accounts for {fmt_pct(share)} of the total {base}."
        else:
            text = f"{leader} accounts for {fmt_pct(share)} of the total {base} ({fmt_num(total)})."
        if n > 1 and len(grouped) > n:
            text += f" The top {n} together make up {fmt_pct(topn_share)}."
        out.append({"kind": "share", "text": text,
                    "data": {"label": leader, "share": share, "total": total, "shown_total": shown_total,
                             "top_n_share": topn_share, "n": n, "basis": basis,
                             "groups_shown": int(len(grouped))}})
    return out


def _column_shares(df: pd.DataFrame, labels: pd.Series, share_col: str) -> pd.Series | None:
    """Per-group share (0..1) from a SQL-computed share column; percent scale when the name says pct/percent or
    any value exceeds 1. None when the column is unusable (missing values, negative, sums above 100%)."""
    raw = _num(df[share_col])
    if raw.isna().any() or bool((raw < 0).any()):
        return None
    scale = 100.0 if (_PERCENT_NAME_RE.search(str(share_col)) or float(raw.max()) > 1.0) else 1.0
    per_group = (raw / scale).groupby(labels, sort=False).sum()
    if float(per_group.sum()) > 1.0 + 1e-3 * len(per_group):  # rounding slack
        return None
    return per_group


def _time_insights(df: pd.DataFrame, tcol: str, measure: str, *, today: date,
                   growth_col: str | None = None) -> list[dict]:
    t = pd.to_datetime(df[tcol], errors="coerce", utc=False)
    frame = pd.DataFrame({"t": t, "v": _num(df[measure])}).dropna().sort_values("t")
    frame = frame.groupby("t", as_index=False)["v"].sum()
    if frame.empty:
        return []
    grain = detect_grain(list(frame["t"]))
    frame = frame.assign(label=[bucket_label(x, grain) for x in frame["t"]])
    partial = None
    if grain is not None and len(frame) >= 2:
        last_t = pd.Timestamp(frame["t"].iloc[-1])
        today_ts = pd.Timestamp(today)
        if last_t <= today_ts < _next_bucket(last_t, grain):
            partial = frame.iloc[-1]
            frame = frame.iloc[:-1]
    name = human_measure(measure)
    out: list[dict] = []
    if len(frame) >= 2:
        last, prev = frame.iloc[-1], frame.iloc[-2]
        ins = _change_insight(measure, last["label"], float(last["v"]), prev["label"], float(prev["v"]))
        if partial is not None:
            ins["text"] += f" {_cap(partial['label'])} is still in progress ({fmt_num(float(partial['v']))} so far)."
            ins["data"]["in_progress"] = {"label": partial["label"], "value": float(partial["v"])}
        out.append(ins)
    if len(frame) >= MIN_TREND_POINTS:
        y = frame["v"].to_numpy(dtype=float)
        x = np.arange(len(y), dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        fitted = slope * x + intercept
        ss_res = float(np.sum((y - fitted) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        mean = float(np.mean(y))
        rel = slope / abs(mean) if mean else None
        first_label, last_label = frame["label"].iloc[0], frame["label"].iloc[-1]
        overall = _pct_change(float(y[-1]), float(y[0]))
        if r2 >= TREND_R2 and abs(slope) > 1e-12:
            direction = "upward" if slope > 0 else "downward"
            text = (f"{_cap(name)} shows a steady {direction} trend over {len(y)} periods "
                    f"(about {fmt_num(abs(slope))} per period"
                    + (f", {fmt_pct(abs(rel))} of the average" if rel is not None else "") + f"; R² {r2:.2f}).")
        else:
            direction = "none"
            text = f"{_cap(name)} shows no clear trend over {len(y)} periods (R² {r2:.2f})."
        if overall is not None and overall != 0:
            text += (f" Overall it {'rose' if overall > 0 else 'fell'} {fmt_pct(abs(overall))} from "
                     f"{first_label} to {last_label}.")
        out.append({"kind": "trend", "text": text,
                    "data": {"measure": measure, "slope_per_period": float(slope), "relative_slope": rel,
                             "r2": r2, "points": int(len(y)), "direction": direction,
                             "overall_change_pct": overall, "first_label": first_label, "last_label": last_label,
                             "grain": grain}})
    if growth_col is not None:
        g = _growth_insight(df, tcol, growth_col, grain, partial)
        if g:
            out.append(g)
    out.extend(_anomalies(frame.rename(columns={"v": measure}), measure, label_col="label"))
    return out


def _growth_insight(df: pd.DataFrame, tcol: str, growth_col: str, grain: str | None, partial) -> dict | None:
    """Largest rise / drop of a period-over-period % column (e.g. growth_pct from the builder)."""
    frame = pd.DataFrame({"t": pd.to_datetime(df[tcol], errors="coerce"), "g": _num(df[growth_col])}).dropna()
    if partial is not None:
        frame = frame[frame["t"] != partial["t"]]
    if len(frame) < 2:
        return None
    frame = frame.assign(label=[bucket_label(x, grain) for x in frame["t"]])
    hi, lo = frame.loc[frame["g"].idxmax()], frame.loc[frame["g"].idxmin()]
    parts = []
    if hi["g"] > 0:
        parts.append(f"the largest increase was {hi['g']:.1f}% in {hi['label']}")
    if lo["g"] < 0:
        parts.append(f"the largest drop was {abs(lo['g']):.1f}% in {lo['label']}")
    if not parts:
        return None
    return {"kind": "growth", "text": _cap("; ".join(parts)) + ".",
            "data": {"max": {"label": hi["label"], "pct": float(hi["g"])},
                     "min": {"label": lo["label"], "pct": float(lo["g"])}}}


def _anomalies(df: pd.DataFrame, measure: str, label_col: str) -> list[dict]:
    """Robust z-score (median / MAD; plain z-score when MAD is 0) outliers with |z| >= 3; needs >= 8 points."""
    values = _num(df[measure])
    mask = values.notna()
    values = values[mask]
    if len(values) < MIN_ANOMALY_POINTS:
        return []
    labels = df.loc[mask, label_col].map(lambda v: v if isinstance(v, str) else _label(v))
    med = float(values.median())
    mad = float((values - med).abs().median())
    if mad == 0:
        std = float(values.std(ddof=0))
        if std == 0:
            return []
        z = (values - float(values.mean())) / std
    else:
        z = 0.6745 * (values - med) / mad
    flagged = z[z.abs() >= ANOMALY_Z].sort_values(key=lambda s: -s.abs())
    out = []
    for idx in flagged.index[:2]:
        val, zz = float(values[idx]), float(z[idx])
        word = "unusually high" if zz > 0 else "unusually low"
        out.append({"kind": "anomaly",
                    "text": f"{labels[idx]} is {word} at {fmt_num(val)} (typical value {fmt_num(med)}).",
                    "data": {"label": labels[idx], "value": val, "median": med, "z": zz, "measure": measure}})
    return out


# ------------------------------------------------------------------------------------------- narration
def narrate_template(question: str, insights: list[dict], *, row_count: int | None = None) -> str:
    """Plain-English narrative from computed insights (no LLM)."""
    if not insights:
        if row_count == 0:
            return "The query returned no rows for this question."
        if row_count is not None:
            return f"The query returned {row_count:,} row{'s' if row_count != 1 else ''}."
        return "Here are the results."
    order = {"summary": 0, "period_change": 1, "top_n": 2, "share": 3, "trend": 4, "growth": 5, "anomaly": 6}
    parts = [i["text"] for i in sorted(insights, key=lambda i: order.get(i.get("kind"), 9))]
    return " ".join(parts)


def narrate_with_gemini(question: str, columns: Sequence, rows: Sequence[Sequence], insights: list[dict], *,
                        dataset_id, user_id=None, max_rows: int = 50) -> Iterator[str]:
    """Stream a Gemini narration. The payload declares SCHEMA + VALUES for dataset:<id>; the policy gate in
    apps.ai raises AIPolicyBlocked BEFORE any network call when VALUES are not allowed for the asset."""
    from apps.ai.services import gemini, get_prompt
    from nexora.contracts import PayloadClass, PayloadManifest

    spec = get_prompt("bi.narrate")
    names = [n for n in _column_names(columns) if n not in _masked(columns)]
    idx = [i for i, n in enumerate(_column_names(columns)) if n in names]
    sample = [[r[i] for i in idx] for r in list(rows)[:max_rows]]
    ref = f"dataset:{dataset_id}"
    payload = PayloadManifest({PayloadClass.SCHEMA: (ref,), PayloadClass.VALUES: (ref,)})
    prompt = spec.render(question=question, columns=names, rows=sample, insights=insights)
    return gemini().stream(feature="bi.narrate", prompt_key=spec.key, system=spec.system, prompt=prompt,
                           payload=payload, tier=spec.tier, priority="assistive", user_id=user_id)


def narrate(question: str, columns: Sequence, rows: Sequence[Sequence], *, dataset_id, user_id=None,
            prefer_gemini: bool = False) -> Iterator[str]:
    """Narration with graceful fallback: Gemini only when requested AND allowed, otherwise the local template."""
    insights = compute_insights(columns, rows)
    if prefer_gemini:
        from apps.ai.services import policy_allows
        from nexora.contracts import PayloadClass, PayloadManifest

        ref = f"dataset:{dataset_id}"
        if policy_allows(PayloadManifest({PayloadClass.SCHEMA: (ref,), PayloadClass.VALUES: (ref,)})):
            started = False
            try:
                for text in narrate_with_gemini(question, columns, rows, insights, dataset_id=dataset_id,
                                                user_id=user_id):
                    started = True
                    yield text
                return
            except Exception:  # noqa: BLE001 - quota / availability: fall back to the local narrative
                if started:
                    return
    yield narrate_template(question, insights, row_count=len(rows))
