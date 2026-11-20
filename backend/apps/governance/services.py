"""Public service API of apps.governance. Other apps import only from here.

    from apps.governance.services import effective_policy, check_dataset_access

    policy = effective_policy(user_id, dataset_id, tables={"sales": "org_ab12.ds_7_v3"},
                              columns_by_table={"sales": ["region", "amount", "email"]})

Semantics (SECURITY_ARCHITECTURE.md §4):
* Dataset ACL: a dataset without ACL rows is open to every member (role permissions still apply); once
  any row exists, the user needs a grant (directly or through their role) whose privilege is at least the
  required one (read < query < export < admin). Owner/admin roles always pass the ACL (they administer it).
* Column policies: `deny` removes the column from `allowed_columns` and WINS over any mask; masks combine
  to the most restrictive (mask_null > mask_hash > mask_partial; mask_partial keeps the smallest keep_last).
* Row policies: every applicable active policy is ANDed. `$user.<attr>` values are bound from
  UserAttribute (plus built-ins `$user.id`, `$user.email`, `$user.role`). Anything that cannot be bound or
  enforced FAILS CLOSED (an empty `in` condition, i.e. no row matches).
* A policy applies to principal `everyone`, the user's role, or the user — unless the user's role id is in
  the policy's `exempt_role_ids`. Owner/admin are NOT implicitly exempt.
* `fingerprint` = sha256 over a canonical JSON of every input (stable across calls with equal inputs).

Contract for the SQL guard: a RowPredicate condition `(col, "in", ())` (empty tuple) must render as FALSE
and `(col, "not_in", ())` as TRUE.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.conf import settings

from apps.core.exceptions import NexoraError
from apps.core.registry import dataset_schema
from apps.core.tenancy import require_org_id
from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

from .models import AccessHistory, ColumnPolicy, DatasetAcl, RowPolicy, UserAttribute

VALID_OPS = frozenset({"=", "!=", "<", "<=", ">", ">=", "in", "not_in", "is_null", "not_null"})
LIST_OPS = frozenset({"in", "not_in"})
NULL_OPS = frozenset({"is_null", "not_null"})
USER_REF = re.compile(r"^\$user\.([a-z_][a-z0-9_]{0,63})$")
COLUMN_RE = re.compile(r"^[^\x00-\x1f\"]{1,128}$")
MAX_CONDITIONS = 20
MAX_LIST_VALUES = 1000
BUILTIN_ATTRS = frozenset({"id", "email", "role"})
ACL_BYPASS_ROLES = frozenset({"owner", "admin"})
PRIVILEGE_RANK = {"read": 1, "query": 2, "export": 3, "admin": 4}
MASK_RANK = {"mask_partial": 1, "mask_hash": 2, "mask_null": 3}
DEFAULT_KEEP_LAST = 4
MAX_ROW_CAP = 5000


class DatasetAccessDenied(NexoraError):
    """The dataset is not visible to the user (404, never 403: existence is not revealed)."""

    status_code = 404
    default_code = "not_found"
    default_detail = "Not found."


# --- predicate validation (used by serializers and at evaluation time) ------------------------------
def _is_scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) and not (isinstance(v, float) and v != v)


def validate_predicate(predicate: Any) -> dict:
    """Validate and normalise `{"all": [{"col", "op", "value"}]}`. Raises ValueError with a message."""
    if not isinstance(predicate, dict) or set(predicate) != {"all"}:
        raise ValueError('Predicate must be an object with a single key "all".')
    conds = predicate["all"]
    if not isinstance(conds, list) or not 1 <= len(conds) <= MAX_CONDITIONS:
        raise ValueError(f'"all" must be a list of 1 to {MAX_CONDITIONS} conditions.')
    out = []
    for i, c in enumerate(conds):
        if not isinstance(c, dict) or not {"col", "op"} <= set(c) or set(c) - {"col", "op", "value"}:
            raise ValueError(f"Condition {i}: expected keys col, op and value.")
        col, op, value = c["col"], c["op"], c.get("value")
        if not isinstance(col, str) or not COLUMN_RE.match(col):
            raise ValueError(f"Condition {i}: invalid column name.")
        if op not in VALID_OPS:
            raise ValueError(f"Condition {i}: unsupported operator {op!r}.")
        if op in NULL_OPS:
            if value is not None:
                raise ValueError(f"Condition {i}: {op} takes no value.")
        elif isinstance(value, str) and value.startswith("$user."):
            if not USER_REF.match(value):
                raise ValueError(f"Condition {i}: invalid user attribute reference {value!r}.")
        elif op in LIST_OPS:
            if not isinstance(value, list) or len(value) > MAX_LIST_VALUES or not all(_is_scalar(v) for v in value):
                raise ValueError(f"Condition {i}: {op} needs a list of up to {MAX_LIST_VALUES} scalar values.")
            if any(isinstance(v, str) and v.startswith("$user.") for v in value):
                raise ValueError(f"Condition {i}: user attribute references cannot appear inside a list.")
        elif not _is_scalar(value):
            raise ValueError(f"Condition {i}: {op} needs a scalar value.")
        out.append({"col": col, "op": op, "value": value})
    return {"all": out}


# --- pure policy computation (no DB) -----------------------------------------------------------------------
@dataclass(frozen=True)
class PolicySubject:
    user_id: UUID
    role_id: UUID | None
    role_key: str
    email: str = ""
    attributes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnRule:
    id: str
    column_name: str
    principal_type: str
    principal_id: UUID | None
    action: str
    params: Mapping[str, Any] = field(default_factory=dict)
    exempt_role_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class RowRule:
    id: str
    principal_type: str
    principal_id: UUID | None
    predicate: Mapping[str, Any]
    exempt_role_ids: tuple[UUID, ...] = ()
    is_active: bool = True
    name: str = ""


def _applies(rule, subject: PolicySubject) -> bool:
    exempt = {str(r) for r in rule.exempt_role_ids}
    if subject.role_id is not None and str(subject.role_id) in exempt:
        return False
    if rule.principal_type == "everyone":
        return True
    if rule.principal_type == "role":
        return subject.role_id is not None and str(rule.principal_id) == str(subject.role_id)
    if rule.principal_type == "user":
        return str(rule.principal_id) == str(subject.user_id)
    return False


def _keep_last(params: Mapping[str, Any]) -> int:
    try:
        k = int(params.get("keep_last", DEFAULT_KEEP_LAST))
    except (TypeError, ValueError):
        return 0  # unparseable -> reveal nothing
    return min(max(k, 0), 64)


def _combine_masks(rules: Iterable[ColumnRule]) -> MaskSpec | None:
    best: MaskSpec | None = None
    for r in rules:
        spec = MaskSpec(kind=r.action, keep_last=_keep_last(r.params) if r.action == "mask_partial" else DEFAULT_KEEP_LAST)
        if best is None or MASK_RANK[spec.kind] > MASK_RANK[best.kind]:
            best = spec
        elif spec.kind == best.kind == "mask_partial" and spec.keep_last < best.keep_last:
            best = spec
    return best


_UNBINDABLE = object()


def _user_values(attr: str, subject: PolicySubject) -> tuple[str, ...] | None:
    if attr == "id":
        return (str(subject.user_id),)
    if attr == "email":
        return (subject.email,) if subject.email else None
    if attr == "role":
        return (subject.role_key,) if subject.role_key else None
    vals = subject.attributes.get(attr)
    return tuple(vals) if vals else None


def _bind_condition(cond: Mapping[str, Any], subject: PolicySubject):
    col, op, value = cond["col"], cond["op"], cond.get("value")
    if op in NULL_OPS:
        return (col, op, None)
    if isinstance(value, str):
        m = USER_REF.match(value)
        if m:
            vals = _user_values(m.group(1), subject)
            if not vals:
                return _UNBINDABLE
            if op in LIST_OPS:
                return (col, op, tuple(vals))
            if len(vals) == 1:
                return (col, op, vals[0])
            if op == "=":
                return (col, "in", tuple(vals))
            if op == "!=":
                return (col, "not_in", tuple(vals))
            return _UNBINDABLE  # range comparison against a multi-valued attribute is ambiguous
    if op in LIST_OPS:
        return (col, op, tuple(value or ()))
    return (col, op, value)


def _deny_all(columns: Iterable[str]) -> tuple[str, str, tuple] | None:
    cols = sorted(columns)
    return (cols[0], "in", ()) if cols else None


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_policy(
    *,
    org_id: UUID,
    dataset_id: UUID,
    subject: PolicySubject,
    tables: Mapping[str, str],
    columns_by_table: Mapping[str, Iterable[str]],
    column_rules: Iterable[ColumnRule] = (),
    row_rules: Iterable[RowRule] = (),
    row_cap: int | None = None,
    byte_cap: int | None = None,
    timeout_s: int | None = None,
) -> QueryPolicy:
    """Pure function: derive the frozen QueryPolicy from already-loaded inputs."""
    row_cap = int(row_cap if row_cap is not None else settings.NL2SQL_ROW_CAP)
    byte_cap = int(byte_cap if byte_cap is not None else settings.NL2SQL_BYTE_CAP)
    timeout_s = int(timeout_s if timeout_s is not None else settings.NL2SQL_TIMEOUT_S)
    tables = {str(k): str(v) for k, v in tables.items()}
    cols = {t: sorted({str(c) for c in columns_by_table.get(t, ())}) for t in tables}
    column_rules = sorted(column_rules, key=lambda r: r.id)
    row_rules = sorted(row_rules, key=lambda r: r.id)

    # columns: deny wins, masks -> most restrictive
    applicable_cols = [r for r in column_rules if _applies(r, subject)]
    denied = {r.column_name for r in applicable_cols if r.action == "deny"}
    masks_by_col: dict[str, list[ColumnRule]] = {}
    for r in applicable_cols:
        if r.action in MASK_RANK:
            masks_by_col.setdefault(r.column_name, []).append(r)
    allowed_columns: dict[str, frozenset[str]] = {}
    column_masks: dict[tuple[str, str], MaskSpec] = {}
    for t, tcols in cols.items():
        allowed = frozenset(c for c in tcols if c not in denied)
        allowed_columns[t] = allowed
        for c in sorted(allowed):
            spec = _combine_masks(masks_by_col.get(c, ()))
            if spec is not None:
                column_masks[(t, c)] = spec

    # rows: AND every applicable predicate; fail closed when it cannot be bound or enforced
    conds_by_table: dict[str, list[tuple]] = {t: [] for t in tables}

    def deny_table(t: str) -> None:
        d = _deny_all(cols[t])
        if d is not None and d not in conds_by_table[t]:
            conds_by_table[t].append(d)

    for rule in row_rules:
        if not rule.is_active or not _applies(rule, subject):
            continue
        try:
            pred = validate_predicate(rule.predicate)
        except ValueError:
            for t in tables:
                deny_table(t)
            continue
        bound = [_bind_condition(c, subject) for c in pred["all"]]
        needed = {c["col"] for c in pred["all"]}
        applied = False
        for t in tables:
            tcols = set(cols[t])
            if needed <= tcols:
                applied = True
                if any(b is _UNBINDABLE for b in bound):
                    deny_table(t)
                else:
                    conds_by_table[t].extend(b for b in bound if b not in conds_by_table[t])
            elif needed & tcols:
                applied = True
                deny_table(t)  # only part of the predicate can be enforced on this table
        if not applied:  # the policy references no column of this dataset: it cannot be enforced
            for t in tables:
                deny_table(t)
    row_predicates = {t: RowPredicate(conditions=tuple(c)) for t, c in conds_by_table.items() if c}

    fingerprint = hashlib.sha256(
        _canonical(
            {
                "v": 1,
                "org_id": str(org_id),
                "dataset_id": str(dataset_id),
                "user_id": str(subject.user_id),
                "role_id": str(subject.role_id) if subject.role_id else None,
                "role_key": subject.role_key,
                "attributes": {k: sorted(v) for k, v in sorted(subject.attributes.items())},
                "email": subject.email,
                "tables": tables,
                "columns": cols,
                "column_rules": [
                    [r.id, r.column_name, r.principal_type, str(r.principal_id) if r.principal_id else None,
                     r.action, dict(r.params), sorted(str(x) for x in r.exempt_role_ids)]
                    for r in column_rules
                ],
                "row_rules": [
                    [r.id, r.principal_type, str(r.principal_id) if r.principal_id else None, r.predicate,
                     sorted(str(x) for x in r.exempt_role_ids), r.is_active]
                    for r in row_rules
                ],
                "caps": [row_cap, byte_cap, timeout_s],
            }
        ).encode()
    ).hexdigest()

    return QueryPolicy(
        org_id=org_id,
        user_id=subject.user_id,
        dataset_id=dataset_id,
        tables=tables,
        allowed_columns=allowed_columns,
        column_masks=column_masks,
        row_predicates=row_predicates,
        row_cap=row_cap,
        byte_cap=byte_cap,
        timeout_s=timeout_s,
        fingerprint=fingerprint,
    )


def policy_to_dict(policy: QueryPolicy) -> dict:
    """JSON-friendly view of a QueryPolicy (policy preview API, audit metadata)."""
    return {
        "org_id": str(policy.org_id),
        "user_id": str(policy.user_id) if policy.user_id else None,
        "dataset_id": str(policy.dataset_id),
        "tables": dict(policy.tables),
        "allowed_columns": {t: sorted(c) for t, c in policy.allowed_columns.items()},
        "column_masks": [
            {"table": t, "column": c, "kind": m.kind, "keep_last": m.keep_last if m.kind == "mask_partial" else None}
            for (t, c), m in sorted(policy.column_masks.items())
        ],
        "row_predicates": {
            t: [{"col": c, "op": op, "value": list(v) if isinstance(v, tuple) else v} for c, op, v in p.conditions]
            for t, p in policy.row_predicates.items()
        },
        "row_cap": policy.row_cap,
        "byte_cap": policy.byte_cap,
        "timeout_s": policy.timeout_s,
        "fingerprint": policy.fingerprint,
    }


def _stricter_mask(a: MaskSpec | None, b: MaskSpec | None) -> MaskSpec | None:
    if a is None or b is None:
        return a or b
    if MASK_RANK[a.kind] != MASK_RANK[b.kind]:
        return a if MASK_RANK[a.kind] > MASK_RANK[b.kind] else b
    return a if a.keep_last <= b.keep_last else b


def intersect_policies(primary: QueryPolicy, other: QueryPolicy) -> QueryPolicy:
    """The policy that satisfies BOTH: allowed columns intersect, masks combine to the most restrictive, row
    predicates are ANDed, caps take the minimum. Used by the policy preview so an admin previewing "as" another
    member never sees a column or row their OWN policy hides. Identity fields come from `primary`."""
    if primary.dataset_id != other.dataset_id or primary.org_id != other.org_id:
        raise ValueError("policies of different datasets cannot be combined")
    allowed: dict[str, frozenset[str]] = {}
    for table, cols in primary.allowed_columns.items():
        both = frozenset(cols) & frozenset(other.allowed_columns.get(table, frozenset()))
        if both:
            allowed[table] = both
    masks: dict[tuple[str, str], MaskSpec] = {}
    for key in set(primary.column_masks) | set(other.column_masks):
        if key[0] in allowed and key[1] in allowed[key[0]]:
            masks[key] = _stricter_mask(primary.column_masks.get(key), other.column_masks.get(key))
    predicates: dict[str, RowPredicate] = {}
    for table in allowed:
        conds = tuple(primary.row_predicates[table].conditions) if table in primary.row_predicates else ()
        if table in other.row_predicates:
            conds += tuple(c for c in other.row_predicates[table].conditions if c not in conds)
        if conds:
            predicates[table] = RowPredicate(conditions=conds)
    fingerprint = hashlib.sha256(f"{primary.fingerprint}|{other.fingerprint}".encode()).hexdigest()
    return QueryPolicy(
        org_id=primary.org_id, user_id=primary.user_id, dataset_id=primary.dataset_id,
        tables={t: p for t, p in primary.tables.items() if t in allowed},
        allowed_columns=allowed, column_masks=masks, row_predicates=predicates,
        row_cap=min(primary.row_cap, other.row_cap), byte_cap=min(primary.byte_cap, other.byte_cap),
        timeout_s=min(primary.timeout_s, other.timeout_s), fingerprint=fingerprint,
    )


# --- DB-backed API (inside a tenant context) -----------------------------------------------------------------
def load_subject(user_id: UUID | str) -> PolicySubject | None:
    """The user's active membership in the current org, or None."""
    from apps.orgs.models import Membership

    m = Membership.objects.select_related("role", "user").filter(user_id=user_id, status="active").first()
    if m is None:
        return None
    attrs = {a.key: tuple(a.values) for a in UserAttribute.objects.filter(user_id=m.user_id)}
    return PolicySubject(user_id=m.user_id, role_id=m.role_id, role_key=m.role.key, email=m.user.email,
                         attributes=attrs)


def _acl_allows(subject: PolicySubject, dataset_id, privilege: str) -> bool:
    if subject.role_key in ACL_BYPASS_ROLES:
        return True
    grants = list(DatasetAcl.objects.filter(dataset_id=dataset_id).values_list("principal_type", "principal_id",
                                                                               "privilege"))
    if not grants:
        return True
    need = PRIVILEGE_RANK[privilege]
    for ptype, pid, priv in grants:
        mine = (ptype == "user" and str(pid) == str(subject.user_id)) or (
            ptype == "role" and subject.role_id is not None and str(pid) == str(subject.role_id)
        )
        if mine and PRIVILEGE_RANK.get(priv, 0) >= need:
            return True
    return False


def check_dataset_access(user_id: UUID | str, dataset_id: UUID | str, privilege: str = "read") -> bool:
    """True when the user (an active member of the current org) passes the dataset ACL for `privilege`."""
    if privilege not in PRIVILEGE_RANK:
        raise ValueError(f"unknown privilege {privilege!r}")
    require_org_id()
    subject = load_subject(user_id)
    return subject is not None and _acl_allows(subject, dataset_id, privilege)


def visible_dataset_ids(user_id: UUID | str, dataset_ids: Iterable[UUID | str], privilege: str = "read") -> set[str]:
    """Filter candidate dataset ids down to those the user may access (one query for all ACLs)."""
    require_org_id()
    ids = {str(d) for d in dataset_ids}
    subject = load_subject(user_id)
    if subject is None or not ids:
        return set()
    if subject.role_key in ACL_BYPASS_ROLES:
        return ids
    need = PRIVILEGE_RANK[privilege]
    restricted: dict[str, bool] = {}
    for ds, ptype, pid, priv in DatasetAcl.objects.filter(dataset_id__in=ids).values_list(
        "dataset_id", "principal_type", "principal_id", "privilege"
    ):
        ok = restricted.get(str(ds), False)
        mine = (ptype == "user" and str(pid) == str(subject.user_id)) or (
            ptype == "role" and str(pid) == str(subject.role_id)
        )
        restricted[str(ds)] = ok or (mine and PRIVILEGE_RANK.get(priv, 0) >= need)
    return {d for d in ids if d not in restricted or restricted[d]}


def _org_row_cap() -> int:
    from apps.orgs.services import get_current_org

    cap = settings.NL2SQL_ROW_CAP
    try:
        override = (get_current_org().settings or {}).get("row_cap")
        if override is not None:
            cap = int(override)
    except (TypeError, ValueError):
        pass
    return min(max(cap, 1), MAX_ROW_CAP)


def effective_policy(
    user_id: UUID | str,
    dataset_id: UUID | str,
    tables: Mapping[str, str] | None = None,
    columns_by_table: Mapping[str, Iterable[str]] | None = None,
    *,
    privilege: str = "query",
) -> QueryPolicy:
    """CONTRACT (nexora.contracts.PolicyService): the frozen QueryPolicy for `user_id` on `dataset_id`.

    `tables` (logical -> physical "schema.table") and `columns_by_table` (logical -> column names) are
    supplied by the caller; when omitted they are resolved through the dataset schema provider registered
    in apps.core.registry. Raises DatasetAccessDenied (404) when the user is not an active member or fails
    the dataset ACL for `privilege`.
    """
    org_id = require_org_id()
    ds_uuid = dataset_id if isinstance(dataset_id, UUID) else UUID(str(dataset_id))
    if tables is None or columns_by_table is None:
        resolved = dataset_schema(ds_uuid)
        if resolved is None:
            raise ValueError("tables and columns_by_table are required (no dataset schema provider registered)")
        tables, columns_by_table = resolved
    subject = load_subject(user_id)
    if subject is None or not _acl_allows(subject, ds_uuid, privilege):
        raise DatasetAccessDenied()
    column_rules = [
        ColumnRule(id=str(p.id), column_name=p.column_name, principal_type=p.principal_type,
                   principal_id=p.principal_id, action=p.action, params=p.params or {},
                   exempt_role_ids=tuple(p.exempt_role_ids or ()))
        for p in ColumnPolicy.objects.filter(dataset_id=ds_uuid)
    ]
    row_rules = [
        RowRule(id=str(p.id), principal_type=p.principal_type, principal_id=p.principal_id,
                predicate=p.predicate, exempt_role_ids=tuple(p.exempt_role_ids or ()), is_active=p.is_active,
                name=p.name)
        for p in RowPolicy.objects.filter(dataset_id=ds_uuid, is_active=True)
    ]
    return compute_policy(
        org_id=org_id,
        dataset_id=ds_uuid,
        subject=subject,
        tables=tables,
        columns_by_table=columns_by_table,
        column_rules=column_rules,
        row_rules=row_rules,
        row_cap=_org_row_cap(),
    )


# --- access history ------------------------------------------------------------------------------------------
def record_access(
    asset_type: str,
    asset_id: UUID | str,
    access_type: str,
    *,
    user_id: UUID | str | None,
    api_key_id: UUID | str | None = None,
    query_audit_id: UUID | str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AccessHistory:
    """Append one access-history row (inside the caller's tenant context). Never stores data values."""
    from django.utils import timezone

    require_org_id()
    return AccessHistory.objects.create(
        occurred_at=timezone.now(),
        user_id=user_id,
        api_key_id=api_key_id,
        asset_type=asset_type,
        asset_id=asset_id if isinstance(asset_id, UUID) else UUID(str(asset_id)),
        access_type=access_type,
        query_audit_id=query_audit_id,
        metadata=dict(metadata or {}),
    )
