"""Public service API of apps.sqlguard (implements nexora.contracts.SqlGuard).

    validate(sql, policy: QueryPolicy) -> GuardResult      pure; never raises; codes from GUARD_REJECT_CODES
    execute(result, policy, target: QueryTarget) -> ResultSet
                                                           raises apps.sqlguard.exceptions.GuardError(code=...)
    validate_expression(expression, columns) -> str        metric expressions (semantic layer), raises GuardReject
    plan_relations(plan) / plan_total_cost(plan)           EXPLAIN (FORMAT JSON) helpers

See SECURITY_ARCHITECTURE.md §5 and apps/sqlguard/guard.py for the pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

import sqlglot
from django.conf import settings
from sqlglot import exp
from sqlglot.errors import SqlglotError

from apps.core.tenancy import get_current_org_id
from nexora.contracts import GuardResult, QueryPolicy, QueryTarget, ResultSet

from .dialect import DIALECT
from .exceptions import GuardError, GuardReject, GuardTimeout
from .guard import check_static
from .guard import validate as _validate
from .verify import verify_output

log = logging.getLogger("nexora.sqlguard")

MAX_CELL_CHARS = 2048
WORK_MEM = "32MB"
MAX_EXPRESSION_CHARS = 2000
EXTERNAL_NOT_AVAILABLE = "External targets arrive with the PostgreSQL connector."


def validate(sql: str, policy: QueryPolicy) -> GuardResult:
    """CONTRACT: parse, validate and rewrite `sql` for `policy` (SECURITY_ARCHITECTURE §5.1)."""
    result = _validate(sql, policy)
    if not result.ok:
        log.info("sqlguard reject code=%s fingerprint=%s", result.reject_code, policy.fingerprint)
    return result


# ------------------------------------------------------------------------------------------------ EXPLAIN
def _plan_nodes(plan: Any) -> Iterator[Mapping[str, Any]]:
    stack = list(plan) if isinstance(plan, list) else [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, Mapping):
            continue
        if isinstance(node.get("Plan"), Mapping):
            stack.append(node["Plan"])
        if "Node Type" in node:
            yield node
        stack.extend(node.get("Plans") or ())


def plan_relations(plan: Any) -> set[tuple[str, str]]:
    """(schema, relation) of every scanned relation; function scans are reported as ("", "function:<name>")."""
    out: set[tuple[str, str]] = set()
    for node in _plan_nodes(plan):
        if "Relation Name" in node:
            out.add((str(node.get("Schema") or ""), str(node["Relation Name"])))
        if "Function Name" in node:
            out.add(("", f"function:{node['Function Name']}"))
    return out


def plan_total_cost(plan: Any) -> float:
    root = plan[0] if isinstance(plan, list) and plan else plan
    if isinstance(root, Mapping) and isinstance(root.get("Plan"), Mapping):
        return float(root["Plan"].get("Total Cost") or 0.0)
    return 0.0


def make_plan_check(policy: QueryPolicy, max_cost: float | None = None):
    """The EXPLAIN check (§5.1 #13): only policy relations, total cost <= NL2SQL_MAX_COST. Returns the cost."""
    allowed = {tuple(str(p).lower().split(".", 1)) for p in policy.tables.values()}
    limit = float(max_cost if max_cost is not None else settings.NL2SQL_MAX_COST)

    def check(plan: Any) -> float:
        if not any(True for _ in _plan_nodes(plan)):
            raise GuardError("The query plan could not be inspected.", code="execution_error")
        for schema, rel in plan_relations(plan):
            if (schema.lower(), rel.lower()) not in allowed:
                log.warning("sqlguard plan relation refused rel=%s.%s fingerprint=%s", schema, rel, policy.fingerprint)
                raise GuardError("The query plan reads a relation outside the policy.",
                                 code="plan_relation_not_allowed", status=403)
        cost = plan_total_cost(plan)
        if cost > limit:
            raise GuardError(f"The query is too expensive (estimated cost {cost:,.0f} > {limit:,.0f}).",
                             code="cost_exceeded", extra={"plan_cost": cost, "max_cost": limit})
        return cost

    return check


# ------------------------------------------------------------------------------------------------ execute
def execute(result: GuardResult, policy: QueryPolicy, target: QueryTarget) -> ResultSet:
    """CONTRACT: run a validated query read-only as the org's reader role (SECURITY_ARCHITECTURE §5.2)."""
    from apps.warehouse import services as warehouse
    from apps.warehouse.exceptions import (
        WarehouseError,
        WarehouseQueryTimeout,
        WarehouseTenantMismatch,
        WarehouseUnavailable,
    )

    if not isinstance(result, GuardResult) or not result.ok or not result.executed_sql:
        raise GuardError("Only a query accepted by the guard can be executed.", code="execution_error")
    if target.kind == "external":
        raise GuardError(EXTERNAL_NOT_AVAILABLE, code="execution_error", status=501)
    if target.kind != "warehouse":
        raise GuardError("Unknown query target.", code="execution_error")
    if UUID(str(target.org_id)) != UUID(str(policy.org_id)):
        log.warning("sqlguard refused cross-org execution target=%s policy=%s", target.org_id, policy.org_id)
        raise GuardError("The query target belongs to another organization.", code="execution_error", status=403)
    active = get_current_org_id()
    if active is None or UUID(str(active)) != UUID(str(policy.org_id)):
        raise GuardError("The query policy belongs to another organization.", code="execution_error", status=403)
    try:
        verify_output(result.executed_sql, result.params, policy)
    except GuardReject as rej:
        log.error("sqlguard refused a GuardResult that fails the output invariants: %s", rej.message)
        raise GuardError(rej.message, code="execution_error", status=403) from rej

    try:
        rs = warehouse.execute_readonly_sql(
            policy.org_id,
            result.executed_sql,
            dict(result.params),
            timeout_s=policy.timeout_s,
            row_cap=policy.row_cap,
            byte_cap=policy.byte_cap,
            search_path="pg_catalog",
            work_mem=WORK_MEM,
            plan_check=make_plan_check(policy),
            max_cell_chars=MAX_CELL_CHARS,
        )
    except GuardError:
        raise
    except WarehouseQueryTimeout as exc:
        raise GuardTimeout() from exc
    except WarehouseTenantMismatch as exc:
        raise GuardError("The query policy belongs to another organization.", code="execution_error",
                         status=403) from exc
    except WarehouseUnavailable as exc:
        raise GuardError("The data warehouse is temporarily unavailable.", code="execution_error",
                         status=503) from exc
    except WarehouseError as exc:
        raise GuardError(f"The query failed: {exc.detail}", code="execution_error") from exc
    masked = {col for (_, col) in policy.column_masks}
    columns = tuple(replace(c, masked=c.name in masked) for c in rs.columns)
    return replace(rs, columns=columns)


# ------------------------------------------------------------------------------------------------ expressions
def validate_expression(expression: str, columns: Iterable[str], *, require_aggregate: bool = True) -> str:
    """Validate a semantic-layer metric expression (e.g. ``SUM(amount) - SUM(discount)``).

    The expression must parse as one scalar expression over `columns`, use only allowlisted functions and casts,
    contain no subquery or window, and (by default) aggregate. Returns the normalized SQL. Raises GuardReject.
    """
    text = (expression or "").strip()
    if not text:
        raise GuardReject("parse_error", "The expression is empty.")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise GuardReject("too_large", f"The expression is longer than {MAX_EXPRESSION_CHARS} characters.")
    if ";" in text or "--" in text or "/*" in text:
        raise GuardReject("parse_error", "The expression may not contain ';' or comments.")
    try:
        statements = [s for s in sqlglot.parse(f"SELECT {text} AS m", read=DIALECT) if s is not None]
    except SqlglotError as exc:
        raise GuardReject("parse_error", "The expression could not be parsed.") from exc
    node = statements[0] if len(statements) == 1 else None
    if not isinstance(node, exp.Select) or len(node.expressions) != 1 or node.args.get("from_") is not None:
        raise GuardReject("invalid_sql", "The expression must be a single value.")
    for key, value in node.args.items():
        if value and key not in ("expressions",):
            raise GuardReject("invalid_sql", "The expression must be a single value.")
    check_static(node)
    expr = node.expressions[0]
    expr = expr.this if isinstance(expr, exp.Alias) else expr
    if expr.find(exp.Select, exp.Subquery) is not None:
        raise GuardReject("invalid_sql", "Subqueries are not allowed in expressions.")
    if expr.find(exp.Window) is not None:
        raise GuardReject("invalid_sql", "Window functions are not allowed in metrics.")
    for star in expr.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            raise GuardReject("invalid_sql", "'*' is only allowed as COUNT(*).")
    known = {c.lower() for c in columns}
    for col in expr.find_all(exp.Column):
        if col.table:
            raise GuardReject("column_not_allowed", "Use unqualified column names in expressions.")
        if col.name.lower() not in known:
            raise GuardReject("unknown_column", f"Unknown column {col.name!r}.")
    if require_aggregate and expr.find(exp.AggFunc) is None:
        raise GuardReject("invalid_sql", "A metric must aggregate (for example SUM(amount)).")
    return expr.sql(dialect=DIALECT)
