"""SQL-guard corpus loader and the independent oracle used to check accepted (rewritten) queries.

The oracle re-parses `GuardResult.executed_sql` with sqlglot and checks invariants that must hold for EVERY
accepted query, independently of how the guard is implemented:
  * exactly one statement, rooted in a SELECT or a set operation of SELECTs, with no DML/DDL/command node;
  * every physical table is one of the policy's tables (CTE references excluded; no 3-part names);
  * no denied column is referenced anywhere;
  * every reference to a table with a row predicate sits in a SELECT whose WHERE carries the predicate
    (an empty `in` list must be rendered as FALSE; an empty `not_in` list needs no filter);
  * a masked column is only read from its physical table through its mask expression (masking at the source),
    never raw in the projection, GROUP BY, ORDER BY or a non-policy WHERE condition of that source SELECT;
  * `mask_null` is accepted in exactly two renderings: a NULL that does not mention the column at all
    (`CAST(NULL AS <type>)`), or the Integrator-approved `CASE WHEN FALSE THEN col END`
    (SECURITY_ARCHITECTURE §5 table, CONTRACT "W2 as-built additions"): a single WHEN whose condition is the
    literal FALSE, the column as its THEN, and no ELSE. The planner folds that to a typed NULL constant, so the
    column is never read. Any other CASE shape (TRUE, `1 = 0`, an ELSE, a second WHEN) is a raw read.

Reject codes are the W2 contract set `nexora.contracts.GUARD_REJECT_CODES`.
"""

from __future__ import annotations

import importlib
import re
import uuid
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from nexora.contracts import GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES

CORPUS_PATH = Path(__file__).resolve().parent.parent / "tests" / "security" / "sql" / "corpus.yaml"
KNOWN_CODES: frozenset[str] = GUARD_REJECT_CODES
SECURITY_CODES: frozenset[str] = GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES
# Expression markers of the rewriter's mask expressions (SECURITY_ARCHITECTURE §5.1 "Masking at the source").
MASK_MARKERS: dict[str, tuple[str, ...]] = {
    "mask_partial": ("right(", "repeat(", "overlay(", "substr", "lpad("),
    "mask_hash": ("sha256", "md5(", "digest(", "hashtext"),
    "mask_null": (),  # the raw column must not be read at all
}


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


def _condition(cond) -> tuple[str, str, Any]:
    column, op, value = cond
    if isinstance(value, list):  # in / not_in values are tuples in the contract
        value = tuple(value)
    return (column, op, value)


def build_policy(name: str, *, org_id: uuid.UUID | None = None):
    """A frozen nexora.contracts.QueryPolicy for the named corpus policy."""
    from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

    spec = policy_spec(name)
    masks = {}
    for key, kind in spec["column_masks"].items():
        table, column = key.split(".", 1)
        masks[(table, column)] = MaskSpec(kind=kind)
    preds = {
        t: RowPredicate(conditions=tuple(_condition(c) for c in conds))
        for t, conds in spec["row_predicates"].items()
    }
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


def _parse_all(sql: str) -> list:
    import sqlglot

    return [t for t in sqlglot.parse(_PARAM_RE.sub("NULL", sql), read="postgres") if t is not None]


def _statement_violations(trees: list) -> list[str]:
    from sqlglot import exp

    if len(trees) != 1:
        return [f"executed_sql contains {len(trees)} statements"]
    tree = trees[0]
    problems = []
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
        problems.append(f"root is {type(tree).__name__}, not a SELECT")
    forbidden = tuple(
        getattr(exp, n)
        for n in (
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Drop",
            "Alter",
            "Command",
            "Into",
            "Lock",
            "Set",
            "Transaction",
            "Copy",
            "TruncateTable",
            "Use",
            "Grant",
        )
        if hasattr(exp, n)
    )
    for node in tree.walk():
        if isinstance(node, forbidden):
            problems.append(f"forbidden node {type(node).__name__} in executed_sql")
    for cte in tree.find_all(exp.With):
        if cte.args.get("recursive"):
            problems.append("recursive CTE in executed_sql")
    return problems


def _source_selects(tree, physical: str):
    """(table node, enclosing SELECT) for every reference to `physical` ("schema.table")."""
    from sqlglot import exp

    for t in tree.find_all(exp.Table):
        if f"{(t.db or '').lower()}.{t.name.lower()}" == physical:
            yield t, t.find_ancestor(exp.Select)


def _is_false(node) -> bool:
    from sqlglot import exp

    return isinstance(node, exp.Boolean) and node.this is False


def _row_predicate_violations(tree, spec: dict) -> list[str]:
    problems = []
    for logical, conds in spec["row_predicates"].items():
        phys = spec["tables"][logical].lower()
        deny_all = any(op == "in" and len(list(value or ())) == 0 for _c, op, value in conds)
        pred_cols = {
            c.lower()
            for c, op, value in conds
            if not (op in ("in", "not_in") and len(list(value or ())) == 0)
        }
        for _t, select in _source_selects(tree, phys):
            where = select.args.get("where") if select is not None else None
            if deny_all:
                if where is None or not any(_is_false(n) for n in where.walk()):
                    problems.append(f"deny-all row policy (empty IN) not rendered as FALSE on {phys}")
                continue
            if not pred_cols:
                continue
            where_cols = (
                {c.name.lower() for c in where.find_all(_column_type())} if where is not None else set()
            )
            if not pred_cols <= where_cols:
                problems.append(f"row policy not applied to a reference of {phys}")
    return problems


def _column_type():
    from sqlglot import exp

    return exp.Column


def is_mask_null_rendering(col) -> bool:
    """True when `col` is the THEN of exactly `CASE WHEN FALSE THEN col END` (the approved mask_null form)."""
    from sqlglot import exp

    branch = col.parent
    if not isinstance(branch, exp.If) or col.arg_key != "true" or not _is_false(branch.this):
        return False
    case = branch.parent
    if not isinstance(case, exp.Case) or case.args.get("this") is not None or case.args.get("default") is not None:
        return False
    return len(case.args.get("ifs") or []) == 1


def _masked_refs(node, select, table_node, masked: dict[str, str]):
    """Columns in `node` that read a masked column of `table_node` directly in `select` (its source SELECT)."""
    from sqlglot import exp

    alias = (table_node.alias_or_name or "").lower()
    single_source = len(list(select.find_all(exp.Table))) == 1
    for col in node.find_all(exp.Column):
        if col.name.lower() not in masked:
            continue
        if col.table and col.table.lower() not in (alias, table_node.name.lower()):
            continue
        if not col.table and not single_source:
            continue
        # only references that resolve to THIS select (not to a nested subquery's own source)
        if col.find_ancestor(exp.Select) is not select:
            continue
        # the approved mask_null rendering never reads the column (constant-false branch, folded by the planner)
        if masked[col.name.lower()] == "mask_null" and is_mask_null_rendering(col):
            continue
        yield col


def _mask_violations(tree, spec: dict) -> list[str]:
    """A masked column may be read from its physical table only inside its mask expression."""
    from sqlglot import exp

    problems = []
    by_table: dict[str, dict[str, str]] = {}
    for key, kind in spec["column_masks"].items():
        table, column = key.split(".", 1)
        by_table.setdefault(table, {})[column.lower()] = kind
    for logical, masked in by_table.items():
        phys = spec["tables"][logical].lower()
        policy_cols = {c[0].lower() for c in spec["row_predicates"].get(logical, [])}
        for table_node, select in _source_selects(tree, phys):
            if select is None:
                continue
            source = (select, table_node, masked)
            for projection in select.expressions:
                for col in _masked_refs(projection, *source):
                    kind = masked[col.name.lower()]
                    rendered = projection.sql(dialect="postgres").lower()
                    markers = MASK_MARKERS.get(kind, ())
                    if (
                        not markers
                        or not any(m in rendered for m in markers)
                        or isinstance(projection.unalias(), exp.Column)
                    ):
                        problems.append(
                            f"masked column {logical}.{col.name} ({kind}) read raw: {projection.sql()}"
                        )
            for clause in ("group", "order", "having", "joins"):
                part = select.args.get(clause)
                for node in part if isinstance(part, list) else ([part] if part is not None else []):
                    for col in _masked_refs(node, *source):
                        problems.append(f"masked column {logical}.{col.name} used raw in {clause.upper()}")
            where = select.args.get("where")
            if where is not None:
                for col in _masked_refs(where, *source):
                    if col.name.lower() not in policy_cols:
                        problems.append(f"masked column {logical}.{col.name} filtered raw in WHERE")
    return problems


def oracle_violations(result: Any, policy_name: str, checks: dict | None = None) -> list[str]:
    """Invariant violations of an ACCEPTED GuardResult (empty list = safe)."""
    from sqlglot import exp

    spec = policy_spec(policy_name)
    problems: list[str] = []
    sql = getattr(result, "executed_sql", None)
    if not sql:
        return ["accepted result has no executed_sql"]
    if getattr(result, "reject_code", None):
        problems.append(f"accepted result carries reject_code {result.reject_code!r}")
    try:
        trees = _parse_all(sql)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a finding
        return [f"executed_sql is not parseable as postgres: {exc}"]
    problems += _statement_violations(trees)
    if not trees:
        return problems
    tree = trees[0]

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
    for fn in tree.find_all(exp.Anonymous):
        if str(fn.name).lower().startswith(("pg_", "dblink", "lo_", "set_config", "current_setting")):
            problems.append(f"system function in executed_sql: {fn.sql()}")

    denied = {c.lower() for cols in spec["denied_columns"].values() for c in cols}
    for col in tree.find_all(exp.Column):
        if col.name.lower() in denied:
            problems.append(f"denied column referenced: {col.sql()}")

    problems += _row_predicate_violations(tree, spec)
    problems += _mask_violations(tree, spec)

    allowed_names = {n.lower() for n in spec["tables"]} | physical
    for name in getattr(result, "tables", ()) or ():
        if str(name).lower() not in allowed_names:
            problems.append(f"GuardResult.tables lists a table outside the policy: {name!r}")

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
        if value not in list(params.values()):  # policy predicates are bound parameters (§5.1 step 10)
            problems.append(f"expected bound parameter {value!r}, got {dict(params)!r}")
    return problems
