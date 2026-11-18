"""Golden NL->SQL evaluation over testdata/generated/sales.csv (QA/Security-owned).

    python -m testing.golden                      # builder-only report (run from backend/)
    python -m testing.golden --with-cassettes     # + recorded Gemini cassettes (backend/tests/ai_eval/cassettes)

Every question in `tests/ai_eval/golden_sales.yaml` carries an expected RESULT check (never an exact SQL string).
The expected answer is computed independently in Python from the CSV; the candidate SQL (builder, or a recorded
Gemini proposal replayed through apps.nl2sql) is executed on an in-memory SQLite copy of the same CSV after a
small Postgres->SQLite translation (date_trunc, percentile_cont, DATE literals) and the two results are compared.

Result comparison is positional, like the BI chat result: the first column is the group key (dimension, time
bucket or comparison period) and the last column is the value. NULL group keys are part of the expected result
(GROUP BY keeps them), exactly as Postgres returns them.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import yaml

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
GOLDEN_PATH = BACKEND / "tests" / "ai_eval" / "golden_sales.yaml"
CASSETTE_DIR = BACKEND / "tests" / "ai_eval" / "cassettes" / "nl2sql"
ABS_TOL = 0.01
REL_TOL = 1e-6


# ------------------------------------------------------------------------------------------------ data
@cache
def golden() -> dict:
    with GOLDEN_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _typed(value: str, typ: str):
    if value == "":
        return None
    if typ in ("bigint", "integer"):
        return int(value)
    if typ in ("numeric", "double precision", "real"):
        return float(value)
    return value  # text, date (ISO strings compare correctly)


@cache
def sales_rows() -> tuple[dict, ...]:
    spec = golden()["dataset"]
    types = {c["name"]: c["type"] for c in spec["columns"]}
    path = REPO / spec["file"]
    with path.open(encoding="utf-8", newline="") as fh:
        return tuple({k: _typed(v, types[k]) for k, v in row.items()} for row in csv.DictReader(fh))


def today() -> date:
    return date.fromisoformat(str(golden()["dataset"]["today"]))


# ------------------------------------------------------------------------------------------------ periods
def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def unit_start(d: date, unit: str) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    if unit == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    if unit == "year":
        return date(d.year, 1, 1)
    raise ValueError(unit)


def shift_unit(d: date, unit: str, n: int) -> date:
    if unit == "day":
        return d + timedelta(days=n)
    if unit == "week":
        return d + timedelta(weeks=n)
    if unit == "month":
        return _add_months(d, n)
    if unit == "quarter":
        return _add_months(d, 3 * n)
    if unit == "year":
        return date(d.year + n, 1, 1)
    raise ValueError(unit)


def period_bounds(spec: dict | None) -> tuple[date | None, date | None]:
    """{relative: last, unit: month} | {start: 2025-01-01, end: 2026-01-01} -> half-open [start, end)."""
    if not spec:
        return None, None
    if "relative" in spec:
        offset = {"this": 0, "last": -1}[spec["relative"]]
        start = shift_unit(unit_start(today(), spec["unit"]), spec["unit"], offset)
        return start, shift_unit(start, spec["unit"], 1)
    start = spec.get("start")
    end = spec.get("end")
    return (date.fromisoformat(str(start)) if start else None, date.fromisoformat(str(end)) if end else None)


def bucket(value: str | None, grain: str) -> str | None:
    if value is None:
        return None
    return unit_start(date.fromisoformat(str(value)[:10]), grain).isoformat()


# ------------------------------------------------------------------------------------------------ oracle
def _aggregate(agg: str, values: list) -> Any:
    present = [v for v in values if v is not None]
    if agg == "count_rows":
        return len(values)
    if agg == "count":
        return len(present)
    if agg == "count_distinct":
        return len(set(present))
    if not present:
        return None
    if agg == "sum":
        return math.fsum(present)
    if agg == "avg":
        return math.fsum(present) / len(present)
    if agg == "min":
        return min(present)
    if agg == "max":
        return max(present)
    if agg == "median":
        return statistics.median(present)
    raise ValueError(agg)


def _metric(check: dict, rows: list[dict]) -> Any:
    if check.get("metric") == "aov":
        revenue = _aggregate("sum", [r["revenue"] for r in rows])
        orders = _aggregate("count_distinct", [r["order_id"] for r in rows])
        return (revenue or 0) / orders if orders else None
    measure = check.get("measure")
    return _aggregate(check["agg"], [r.get(measure) if measure else 1 for r in rows])


def _filtered(check: dict, rows) -> list[dict]:
    start, end = period_bounds(check.get("period"))
    out = []
    for r in rows:
        d = r["order_date"]
        if start and (d is None or d < start.isoformat()):
            continue
        if end and (d is None or d >= end.isoformat()):
            continue
        if any(r.get(k) != v for k, v in (check.get("where") or {}).items()):
            continue
        out.append(r)
    return out


@dataclass
class Expected:
    kind: str                         # scalar | mapping | ranking
    value: Any = None                 # scalar
    mapping: dict = field(default_factory=dict)  # key -> value (group / series / compare)
    order: list = field(default_factory=list)    # ranking: ordered keys


def expected_result(check: dict) -> Expected:
    rows = _filtered(check, sales_rows())
    kind = check["type"]
    if kind == "total":
        return Expected("scalar", value=_metric(check, rows))
    if kind in ("group", "series"):
        groups: dict[Any, list[dict]] = defaultdict(list)
        for r in rows:
            key = r.get(check["by"]) if kind == "group" else bucket(r["order_date"], check["grain"])
            groups[key].append(r)
        mapping = {k: _metric(check, g) for k, g in groups.items()}
        if "top" in check:
            reverse = check.get("order", "desc") == "desc"
            ordered = sorted(
                (k for k in mapping if mapping[k] is not None),
                key=lambda k: mapping[k], reverse=reverse,
            )[: int(check["top"])]
            return Expected("ranking", mapping={k: mapping[k] for k in ordered}, order=ordered)
        return Expected("mapping", mapping=mapping)
    if kind == "compare":
        unit = check["unit"]
        cur = unit_start(today(), unit)
        prev = shift_unit(cur, unit, -1)
        nxt = shift_unit(cur, unit, 1)
        current = [r for r in rows if r["order_date"] and cur.isoformat() <= r["order_date"] < nxt.isoformat()]
        previous = [r for r in rows if r["order_date"] and prev.isoformat() <= r["order_date"] < cur.isoformat()]
        return Expected("mapping", mapping={"current_period": _metric(check, current),
                                            "previous_period": _metric(check, previous)})
    raise ValueError(kind)


# ------------------------------------------------------------------------------------------------ sqlite
class _PercentileCont:
    def __init__(self):
        self.values: list[float] = []
        self.p = 0.5

    def step(self, p, value):
        self.p = float(p)
        if value is not None:
            self.values.append(float(value))

    def finalize(self):
        if not self.values:
            return None
        v = sorted(self.values)
        pos = self.p * (len(v) - 1)
        lo, hi = math.floor(pos), math.ceil(pos)
        return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _date_trunc(unit, value):
    if value is None:
        return None
    return unit_start(date.fromisoformat(str(value)[:10]), str(unit).lower()).isoformat()


@cache
def _sqlite_schema() -> tuple[tuple[str, str], ...]:
    sql_types = {"bigint": "INTEGER", "integer": "INTEGER", "numeric": "REAL", "double precision": "REAL"}
    return tuple((c["name"], sql_types.get(c["type"], "TEXT")) for c in golden()["dataset"]["columns"])


def sqlite_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("pg_date_trunc", 2, _date_trunc, deterministic=True)
    conn.create_aggregate("pg_percentile_cont", 2, _PercentileCont)
    table = golden()["dataset"]["table"]
    cols = _sqlite_schema()
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(f"{n} {t}" for n, t in cols)})')
    names = [n for n, _ in cols]
    conn.executemany(
        f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in names)})',
        [tuple(r[n] for n in names) for r in sales_rows()],
    )
    return conn


def to_sqlite(pg_sql: str) -> str:
    """Translate the builder's Postgres subset to SQLite (date_trunc, percentile_cont, DATE literals)."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(pg_sql, read="postgres")

    def rewrite(node):
        if isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            unit = node.args.get("unit")
            unit_text = (unit.name if isinstance(unit, (exp.Var, exp.Literal)) else str(unit)).lower()
            return exp.Anonymous(this="pg_date_trunc", expressions=[exp.Literal.string(unit_text), node.this])
        if isinstance(node, exp.WithinGroup) and isinstance(node.this, (exp.PercentileCont,)):
            ordered = node.expression.find(exp.Ordered)
            return exp.Anonymous(this="pg_percentile_cont", expressions=[node.this.this, ordered.this])
        if isinstance(node, exp.Cast) and node.to.is_type("date") and isinstance(node.this, exp.Literal):
            return exp.Literal.string(node.this.name)
        return node

    return tree.transform(rewrite).sql(dialect="sqlite")


def run_sql(pg_sql: str) -> tuple[list[str], list[tuple]]:
    conn = sqlite_connection()
    try:
        cur = conn.execute(to_sqlite(pg_sql))
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        conn.close()


# ------------------------------------------------------------------------------------------------ compare
def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), rel_tol=REL_TOL, abs_tol=ABS_TOL)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _key(value):
    if value is None:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-" else text


def compare(expected: Expected, columns: list[str], rows: list[tuple]) -> list[str]:
    """Differences between the SQL result and the expected answer (empty list = correct)."""
    if expected.kind == "scalar":
        if len(rows) != 1:
            return [f"expected one row, got {len(rows)}"]
        return [] if _close(rows[0][-1], expected.value) else [f"value {rows[0][-1]!r} != {expected.value!r}"]
    if not columns or len(columns) < 2:
        return [f"expected (key, value) columns, got {columns}"]
    got_order = [_key(r[0]) for r in rows]
    got = {_key(r[0]): r[-1] for r in rows}
    if len(got) != len(rows):
        return ["duplicate group keys in the result"]
    want = {_key(k): v for k, v in expected.mapping.items()}
    problems = []
    if set(got) != set(want):
        problems.append(f"keys differ: missing {sorted(map(str, set(want) - set(got)))}, "
                        f"extra {sorted(map(str, set(got) - set(want)))}")
    for k in set(got) & set(want):
        if not _close(got[k], want[k]):
            problems.append(f"{k}: {got[k]!r} != {want[k]!r}")
    if expected.kind == "ranking" and got_order != [_key(k) for k in expected.order]:
        problems.append(f"ranking {got_order} != {[_key(k) for k in expected.order]}")
    return problems


# ------------------------------------------------------------------------------------------------ context
def question_context():
    """The nexora.contracts.QuestionContext the golden questions are asked against (all columns allowed)."""
    from nexora.contracts import QueryPolicy, QuestionContext

    ds = golden()["dataset"]
    table = ds["table"]
    columns = tuple(
        {k: c[k] for k in ("name", "type", "semantic_type", "role", "description", "synonyms") if k in c}
        for c in ds["columns"]
    )
    org = uuid.UUID("018f0000-0000-7000-8000-00000000e001")
    dataset_id = uuid.UUID("018f0000-0000-7000-8000-00000000e003")
    policy = QueryPolicy(
        org_id=org, user_id=uuid.UUID("018f0000-0000-7000-8000-00000000e002"), dataset_id=dataset_id,
        tables={table: "org_golden.ds_sales_v1"},
        allowed_columns={table: frozenset(c["name"] for c in ds["columns"])},
        row_cap=5000, fingerprint="golden",
    )
    return QuestionContext(
        org_id=org, user_id=policy.user_id, dataset_id=dataset_id, policy=policy,
        columns_meta={table: columns}, metrics=tuple(ds.get("metrics") or ()),
        dataset_name=ds["name"], dataset_classification="synthetic",
    )


# ------------------------------------------------------------------------------------------------ evaluation
@dataclass
class Outcome:
    id: str
    question: str
    source: str            # builder | cassette | none
    correct: bool
    sql: str | None = None
    problems: list[str] = field(default_factory=list)


def builder_sql(question: str) -> str | None:
    from apps.nl2sql.builder import build_sql

    built = build_sql(question, question_context(), today=today())
    return built.sql if built else None


def cassette_for(qid: str) -> dict | None:
    path = CASSETTE_DIR / f"{qid}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def cassette_sql(question: str, cassette: dict) -> str | None:
    """Replay a recorded Gemini response through apps.nl2sql.propose (cache disabled, no network, no DB)."""
    from unittest import mock

    from apps.nl2sql import cache as nl2sql_cache
    from apps.nl2sql import services as nl2sql_services

    from .fakes import FakeGeminiService

    fake = FakeGeminiService().respond(cassette.get("prompt_key", "nl2sql.generate"), cassette["response"])
    with mock.patch.object(nl2sql_cache, "lookup_exact", return_value=None), \
         mock.patch.object(nl2sql_cache, "lookup_semantic", return_value=None), \
         mock.patch.object(nl2sql_services, "build_sql", return_value=None), \
         mock.patch.object(nl2sql_services, "gemini", return_value=fake):
        proposal = nl2sql_services.propose(question, question_context(), today=today())
    return proposal.sql


def evaluate(case: dict, *, use_cassettes: bool = False) -> Outcome:
    question = case["question"]
    sql, source = builder_sql(question), "builder"
    if sql is None and use_cassettes:
        cassette = cassette_for(case["id"])
        if cassette is not None:
            sql, source = cassette_sql(question, cassette), "cassette"
    if sql is None:
        return Outcome(case["id"], question, "none", False, problems=["no SQL (builder declined, no cassette)"])
    try:
        columns, rows = run_sql(sql)
    except Exception as exc:  # noqa: BLE001 - a failing query is an evaluation result, not a crash
        return Outcome(case["id"], question, source, False, sql, [f"execution failed: {exc}"])
    problems = compare(expected_result(case["check"]), columns, rows)
    return Outcome(case["id"], question, source, not problems, sql, problems)


def report(use_cassettes: bool = False) -> dict:
    outcomes = [evaluate(c, use_cassettes=use_cassettes) for c in golden()["questions"]]
    total = len(outcomes)
    answered = [o for o in outcomes if o.source != "none"]
    correct = [o for o in outcomes if o.correct]
    builder_correct = [o for o in outcomes if o.correct and o.source == "builder"]
    return {
        "total": total,
        "answered": len(answered),
        "correct": len(correct),
        "builder_correct": len(builder_correct),
        "builder_accuracy": len(builder_correct) / total if total else 0.0,
        "accuracy": len(correct) / total if total else 0.0,
        "precision_when_answered": len(correct) / len(answered) if answered else 0.0,
        "outcomes": outcomes,
    }


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI convenience
    import argparse
    import os

    import django

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-cassettes", action="store_true")
    args = parser.parse_args(argv)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nexora.settings.test")
    django.setup()
    rep = report(use_cassettes=args.with_cassettes)
    for o in rep["outcomes"]:
        mark = "OK  " if o.correct else ("MISS" if o.source == "none" else "FAIL")
        print(f"{mark} {o.id:<6} [{o.source:<8}] {o.question}" + (f"  -> {o.problems[0]}" if o.problems else ""))
    print(f"\nbuilder accuracy: {rep['builder_correct']}/{rep['total']} = {rep['builder_accuracy']:.1%}; "
          f"answered {rep['answered']}, correct {rep['correct']} "
          f"(precision when answered {rep['precision_when_answered']:.1%})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
