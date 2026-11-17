"""SQL-guard corpus loader and the independent oracle used to check accepted (rewritten) queries.

The oracle re-parses `GuardResult.executed_sql` with sqlglot and checks invariants that must hold for EVERY
accepted query, independently of how the guard is implemented:
  * every physical table is one of the policy's tables (CTE references excluded);
  * no denied column is referenced anywhere;
  * every reference to a table with a row predicate sits in a SELECT whose WHERE mentions the predicate column.
"""

from __future__ import annotations

import importlib
import re
import uuid
from functools import cache
from pathlib import Path
from typing import Any

import yaml

CORPUS_PATH = Path(__file__).resolve().parent.parent / "tests" / "security" / "sql" / "corpus.yaml"
KNOWN_CODES = {
    "too_large", "parse_error", "multiple_statements", "command_not_allowed", "not_select",
    "forbidden_construct", "forbidden_function", "table_not_allowed", "system_catalog", "cte_shadowing",
    "forbidden_type", "system_column", "column_not_allowed", "whole_row_reference", "roundtrip_mismatch",
    "plan_relation_not_allowed", "cost_exceeded",
}
SECURITY_CODES = KNOWN_CODES - {"parse_error", "roundtrip_mismatch"}


@cache
def load_corpus() -> dict:
    with CORPUS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def policy_spec(name: str) -> dict:
    """Resolve a policy (with `extends`) into a flat spec dict."""
    policies = load_corpus()["policies"]
    spec = dict(policies[name])
    parent = spec.pop("extends", None)
    if parent:
        base = policy_spec(parent)
        for key in ("tables", "allowed_columns", "denied_columns", "column_masks", "row_predicates"):
            merged = dict(base.get(key) or {})
            merged.update(spec.get(key) or {})
            spec[key] = merged
    for key in ("tables", "allowed_columns", "denied_columns", "column_masks", "row_predicates"):
        spec.setdefault(key, {})
    return spec


def build_policy(name: str, *, org_id: uuid.UUID | None = None):
    """A frozen nexora.contracts.QueryPolicy for the named corpus policy."""
    from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

    spec = policy_spec(name)
    masks = {}
    for key, kind in spec["column_masks"].items():
        table, column = key.split(".", 1)
        masks[(table, column)] = MaskSpec(kind=kind)
    preds = {t: RowPredicate(conditions=tuple(tuple(c) for c in conds)) for t, conds in spec["row_predicates"].items()}
    return QueryPolicy(
        org_id=org_id or uuid.UUID("018f0000-0000-7000-8000-0000000000a1"),
        user_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a2"),
        dataset_id=uuid.UUID("018f0000-0000-7000-8000-0000000000a3"),
        tables=dict(spec["tables"]),
        allowed_columns={t: frozenset(cols) for t, cols in spec["allowed_columns"].items()},
        column_masks=masks,
        row_predicates=preds,
        fingerprint=f"qa-corpus-{name}",
    )


def case_sql(case: dict) -> str:
    if "sql" in case:
        return case["sql"]
    gen = case["generate"]
    kind = gen["kind"]
    if kind == "nested_subqueries":
        sql = "SELECT region FROM sales"
        for i in range(gen["depth"]):
            sql = f"SELECT region FROM ({sql}) AS n{i}"
        return sql
    if kind == "in_list":
        values = ", ".join(str(i) for i in range(gen["size"]))
        return f"SELECT region FROM sales WHERE order_id IN ({values})"
    if kind == "long_text":
        pad = "x" * gen["bytes"]
        return f"SELECT region FROM sales WHERE product = '{pad}'"
    raise ValueError(f"unknown generator {kind}")


def get_validate():
    """apps.sqlguard.services.validate once the Data Engineering agent ships it, else None."""
    try:
        mod = importlib.import_module("apps.sqlguard.services")
    except ImportError:
        return None
    return getattr(mod, "validate", None)


_PARAM_RE = re.compile(r"%\(\w+\)s|%s|\$\d+")


def _parse(sql: str):
    import sqlglot

    return sqlglot.parse_one(_PARAM_RE.sub("NULL", sql), read="postgres")


def oracle_violations(result: Any, policy_name: str, checks: dict | None = None) -> list[str]:
    """Invariant violations of an ACCEPTED GuardResult (empty list = safe)."""
    from sqlglot import exp

    spec = policy_spec(policy_name)
    problems: list[str] = []
    sql = getattr(result, "executed_sql", None)
    if not sql:
        return ["accepted result has no executed_sql"]
    try:
        tree = _parse(sql)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a finding
        return [f"executed_sql is not parseable as postgres: {exc}"]

    physical = {v.lower() for v in spec["tables"].values()}
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if t.args.get("catalog"):
            problems.append(f"3-part table reference {t.sql()}")
            continue
        db, name = (t.db or "").lower(), t.name.lower()
        if not db and name in cte_names:
            continue
        if f"{db}.{name}" not in physical:
            problems.append(f"table outside policy: {t.sql()}")

    denied = {c.lower() for cols in spec["denied_columns"].values() for c in cols}
    for col in tree.find_all(exp.Column):
        if col.name.lower() in denied:
            problems.append(f"denied column referenced: {col.sql()}")

    for logical, conds in spec["row_predicates"].items():
        phys = spec["tables"][logical].lower()
        pred_cols = {c[0].lower() for c in conds}
        for t in tree.find_all(exp.Table):
            if f"{(t.db or '').lower()}.{t.name.lower()}" != phys:
                continue
            select = t.find_ancestor(exp.Select)
            where = select.args.get("where") if select is not None else None
            where_cols = {c.name.lower() for c in where.find_all(exp.Column)} if where is not None else set()
            if not pred_cols <= where_cols:
                problems.append(f"row policy not applied to a reference of {phys}")

    checks = checks or {}
    lowered = sql.lower()
    for needle in checks.get("contains_ci", []):
        if needle.lower() not in lowered:
            problems.append(f"expected {needle!r} in executed_sql")
    for needle in checks.get("not_contains_ci", []):
        if needle.lower() in lowered:
            problems.append(f"unexpected {needle!r} in executed_sql")
    params = getattr(result, "params", {}) or {}
    for value in checks.get("params_contain", []):
        if value not in list(params.values()):
            problems.append(f"expected bound parameter {value!r}, got {dict(params)!r}")
    return problems
