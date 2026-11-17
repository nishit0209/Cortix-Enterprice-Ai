"""Local insights + templated narration (0 Gemini calls by default) — AI_ARCHITECTURE.md §4 / §6.7.

compute_insights(columns, rows) -> [{kind: top_n|share|period_change|anomaly|trend, text, data}]
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


# ------------------------------------------------------------------------------------------- insights
def compute_insights(columns: Sequence, rows: Sequence[Sequence], *, max_insights: int = 6) -> list[dict]:
    """Deterministic insights from a (<= 1,000 row) result set."""
    if not rows or not columns:
        return []
    df = _frame(columns, rows)
    if df.empty:
        return []
    numeric, temporal, categorical = _classify(df)
    out: list[dict] = []
    if not numeric:
        return out
    measure = numeric[0]
    period_col = next((c for c in categorical if str(c).lower() in PERIOD_NAMES), None)

    # compare-periods shape: rows labelled this_X / last_X
    if period_col is not None and len(df) == 2:
        pc = _period_compare(df, period_col, measure)
        if pc:
            out.append(pc)
            return out[:max_insights]

    # single-row with two numeric columns named this_*/last_* (or current/previous)
    if len(df) == 1 and len(numeric) >= 2:
        pc = _wide_compare(df, numeric)
        if pc:
            out.append(pc)

    if temporal:
        out.extend(_time_insights(df, temporal[0], measure))
    elif categorical:
        out.extend(_category_insights(df, categorical[0], measure))
        out.extend(_anomalies(df, measure, label_col=categorical[0]))
    return out[:max_insights]


def _period_compare(df: pd.DataFrame, period_col: str, measure: str) -> dict | None:
    labels = df[period_col].astype(str).str.lower().tolist()
    cur_idx = next((i for i, lab in enumerate(labels) if lab.startswith(("this", "current"))), None)
    prev_idx = next((i for i, lab in enumerate(labels) if lab.startswith(("last", "previous", "prior"))), None)
    if cur_idx is None or prev_idx is None:
        return None
    cur, prev = float(_num(df[measure]).iloc[cur_idx]), float(_num(df[measure]).iloc[prev_idx])
    return _change_insight(measure, _human(df[period_col].iloc[cur_idx]), cur, _human(df[period_col].iloc[prev_idx]),
                           prev)


def _wide_compare(df: pd.DataFrame, numeric: list[str]) -> dict | None:
    cur_col = next((c for c in numeric if str(c).lower().startswith(("this", "current"))), None)
    prev_col = next((c for c in numeric if str(c).lower().startswith(("last", "previous", "prior"))), None)
    if not cur_col or not prev_col:
        return None
    return _change_insight(cur_col, _human(cur_col), float(_num(df[cur_col]).iloc[0]), _human(prev_col),
                           float(_num(df[prev_col]).iloc[0]))


def _change_insight(measure: str, cur_label: str, cur: float, prev_label: str, prev: float) -> dict:
    delta = cur - prev
    pct = (delta / abs(prev)) if prev not in (0, 0.0) and not math.isnan(prev) else None
    direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    if pct is None:
        text = f"{_human(measure).capitalize()} was {fmt_num(cur)} in {cur_label} versus {fmt_num(prev)} in {prev_label}."
    else:
        verb = {"up": "rose", "down": "fell", "flat": "was unchanged"}[direction]
        change = f" {fmt_pct(abs(pct))}" if direction != "flat" else ""
        text = (f"{_human(measure).capitalize()} {verb}{change} from {fmt_num(prev)} in {prev_label} "
                f"to {fmt_num(cur)} in {cur_label}.")
    return {"kind": "period_change", "text": text,
            "data": {"measure": measure, "current": cur, "previous": prev, "current_label": cur_label,
                     "previous_label": prev_label, "change": delta, "change_pct": pct, "direction": direction}}


def _category_insights(df: pd.DataFrame, dim: str, measure: str) -> list[dict]:
    values = _num(df[measure])
    frame = pd.DataFrame({"dim": df[dim].astype(str), "v": values}).dropna()
    if frame.empty:
        return []
    grouped = frame.groupby("dim", sort=False)["v"].sum().sort_values(ascending=False)
    out: list[dict] = []
    n = min(3, len(grouped))
    top = grouped.head(n)
    items = [{"label": k, "value": float(v)} for k, v in top.items()]
    if len(grouped) >= 2:
        listing = ", ".join(f"{i['label']} ({fmt_num(i['value'])})" for i in items)
        text = f"The top {_human(dim)} by {_human(measure)} {'are' if n > 1 else 'is'} {listing}."
    else:
        text = f"{items[0]['label']} has {_human(measure)} of {fmt_num(items[0]['value'])}."
    out.append({"kind": "top_n", "text": text, "data": {"dimension": dim, "measure": measure, "items": items}})
    total = float(grouped.sum())
    if len(grouped) >= 2 and total > 0 and (grouped >= 0).all():
        leader, lv = grouped.index[0], float(grouped.iloc[0])
        share = lv / total
        topn_share = float(top.sum()) / total
        text = f"{leader} accounts for {fmt_pct(share)} of total {_human(measure)} ({fmt_num(total)})."
        if n > 1 and len(grouped) > n:
            text += f" The top {n} together make up {fmt_pct(topn_share)}."
        out.append({"kind": "share", "text": text,
                    "data": {"label": leader, "share": share, "total": total, "top_n_share": topn_share, "n": n}})
    return out


def _time_insights(df: pd.DataFrame, tcol: str, measure: str) -> list[dict]:
    t = pd.to_datetime(df[tcol], errors="coerce", utc=False)
    frame = pd.DataFrame({"t": t, "v": _num(df[measure])}).dropna().sort_values("t")
    frame = frame.groupby("t", as_index=False)["v"].sum()
    out: list[dict] = []
    if len(frame) >= 2:
        last, prev = frame.iloc[-1], frame.iloc[-2]
        out.append(_change_insight(measure, _label(last["t"]), float(last["v"]), _label(prev["t"]), float(prev["v"])))
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
        if r2 >= TREND_R2 and slope != 0:
            direction = "upward" if slope > 0 else "downward"
            text = (f"{_human(measure).capitalize()} shows a steady {direction} trend over {len(y)} periods "
                    f"(about {fmt_num(abs(slope))} per period"
                    + (f", {fmt_pct(abs(rel))} of the average" if rel is not None else "") + f"; R² {r2:.2f}).")
        else:
            direction = "none"
            text = f"{_human(measure).capitalize()} shows no clear trend over {len(y)} periods (R² {r2:.2f})."
        out.append({"kind": "trend", "text": text,
                    "data": {"measure": measure, "slope_per_period": float(slope), "relative_slope": rel,
                             "r2": r2, "points": int(len(y)), "direction": direction}})
    frame = frame.assign(label=frame["t"].map(_label))
    out.extend(_anomalies(frame.rename(columns={"v": measure}), measure, label_col="label"))
    return out


def _anomalies(df: pd.DataFrame, measure: str, label_col: str) -> list[dict]:
    """Robust z-score (median / MAD) outliers; needs >= 8 points."""
    values = _num(df[measure])
    mask = values.notna()
    values = values[mask]
    if len(values) < MIN_ANOMALY_POINTS:
        return []
    labels = df.loc[mask, label_col].map(_label)
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
    order = {"period_change": 0, "top_n": 1, "share": 2, "trend": 3, "anomaly": 4}
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
