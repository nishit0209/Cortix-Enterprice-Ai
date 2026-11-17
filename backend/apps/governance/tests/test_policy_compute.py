"""Pure unit tests of the effective-policy engine (no database)."""

from uuid import UUID, uuid4

import pytest

from apps.governance.services import (
    ColumnRule,
    PolicySubject,
    RowRule,
    compute_policy,
    validate_predicate,
)
from nexora.contracts import MaskSpec, QueryPolicy, RowPredicate

ORG = UUID("01900000-0000-7000-8000-000000000001")
DS = UUID("01900000-0000-7000-8000-0000000000d5")
ANALYST_ROLE = UUID("01900000-0000-7000-8000-0000000000a1")
OWNER_ROLE = UUID("01900000-0000-7000-8000-0000000000b1")
USER = UUID("01900000-0000-7000-8000-0000000000c1")
OTHER_USER = UUID("01900000-0000-7000-8000-0000000000c2")

TABLES = {"sales": "org_ab12cd34.ds_1_v1", "regions": "org_ab12cd34.ds_1_v1_regions"}
COLUMNS = {"sales": ["order_id", "region", "amount", "email", "phone"], "regions": ["region", "manager"]}


def subject(role_id=ANALYST_ROLE, role_key="analyst", attributes=None, user_id=USER, email="ana@example.test"):
    return PolicySubject(user_id=user_id, role_id=role_id, role_key=role_key, email=email,
                         attributes=attributes or {})


def col_rule(column, action, ptype="everyone", pid=None, params=None, exempt=(), rid=None):
    return ColumnRule(id=rid or str(uuid4()), column_name=column, principal_type=ptype, principal_id=pid,
                      action=action, params=params or {}, exempt_role_ids=tuple(exempt))


def row_rule(conds, ptype="everyone", pid=None, exempt=(), active=True, rid=None):
    return RowRule(id=rid or str(uuid4()), principal_type=ptype, principal_id=pid, predicate={"all": conds},
                   exempt_role_ids=tuple(exempt), is_active=active)


def compute(subj=None, column_rules=(), row_rules=(), tables=TABLES, columns=COLUMNS, **caps):
    caps = {"row_cap": 1000, "byte_cap": 5_000_000, "timeout_s": 15, **caps}
    return compute_policy(org_id=ORG, dataset_id=DS, subject=subj or subject(), tables=tables,
                          columns_by_table=columns, column_rules=column_rules, row_rules=row_rules, **caps)


# --- shape / defaults -----------------------------------------------------------------------------------------
def test_no_rules_allows_every_column_and_returns_contract_type():
    p = compute()
    assert isinstance(p, QueryPolicy)
    assert p.org_id == ORG and p.dataset_id == DS and p.user_id == USER
    assert dict(p.tables) == TABLES
    assert p.allowed_columns == {"sales": frozenset(COLUMNS["sales"]), "regions": frozenset(COLUMNS["regions"])}
    assert p.column_masks == {} and p.row_predicates == {}
    assert (p.row_cap, p.byte_cap, p.timeout_s) == (1000, 5_000_000, 15)
    assert len(p.fingerprint) == 64


def test_table_without_known_columns_allows_nothing():
    p = compute(columns={"sales": COLUMNS["sales"]})
    assert p.allowed_columns["regions"] == frozenset()


def test_caps_default_to_settings(settings):
    settings.NL2SQL_ROW_CAP, settings.NL2SQL_BYTE_CAP, settings.NL2SQL_TIMEOUT_S = 321, 4096, 7
    p = compute_policy(org_id=ORG, dataset_id=DS, subject=subject(), tables=TABLES, columns_by_table=COLUMNS)
    assert (p.row_cap, p.byte_cap, p.timeout_s) == (321, 4096, 7)


# --- column policies ---------------------------------------------------------------------------------------------
def test_deny_removes_column_from_every_table_that_has_it():
    p = compute(column_rules=[col_rule("region", "deny")])
    assert "region" not in p.allowed_columns["sales"]
    assert "region" not in p.allowed_columns["regions"]
    assert "amount" in p.allowed_columns["sales"]


def test_deny_wins_over_mask():
    p = compute(column_rules=[col_rule("email", "mask_hash"), col_rule("email", "deny", "user", USER)])
    assert "email" not in p.allowed_columns["sales"]
    assert ("sales", "email") not in p.column_masks


@pytest.mark.parametrize(
    "actions,expected",
    [
        (["mask_partial", "mask_hash"], "mask_hash"),
        (["mask_hash", "mask_null"], "mask_null"),
        (["mask_partial", "mask_null", "mask_hash"], "mask_null"),
        (["mask_partial"], "mask_partial"),
    ],
)
def test_masks_combine_to_most_restrictive(actions, expected):
    rules = [col_rule("phone", a, "everyone" if i == 0 else "role", None if i == 0 else ANALYST_ROLE,
                      params={"keep_last": 4} if a == "mask_partial" else {}) for i, a in enumerate(actions)]
    # role-scoped duplicates need distinct principals; everyone + role rules may coexist
    p = compute(column_rules=rules)
    assert p.column_masks[("sales", "phone")].kind == expected


def test_mask_partial_keeps_smallest_keep_last():
    p = compute(column_rules=[
        col_rule("phone", "mask_partial", params={"keep_last": 4}),
        col_rule("phone", "mask_partial", "role", ANALYST_ROLE, params={"keep_last": 2}),
    ])
    assert p.column_masks[("sales", "phone")] == MaskSpec(kind="mask_partial", keep_last=2)


def test_mask_partial_with_garbage_keep_last_reveals_nothing():
    p = compute(column_rules=[col_rule("phone", "mask_partial", params={"keep_last": "x"})])
    assert p.column_masks[("sales", "phone")].keep_last == 0


def test_role_and_user_principals_only_apply_to_their_target():
    rules = [
        col_rule("email", "deny", "role", OWNER_ROLE),          # other role: ignored
        col_rule("phone", "deny", "user", OTHER_USER),          # other user: ignored
        col_rule("amount", "mask_null", "role", ANALYST_ROLE),  # my role
        col_rule("region", "mask_hash", "user", USER),          # me
    ]
    p = compute(column_rules=rules)
    assert {"email", "phone"} <= p.allowed_columns["sales"]
    assert p.column_masks[("sales", "amount")].kind == "mask_null"
    assert p.column_masks[("sales", "region")].kind == "mask_hash"
    assert p.column_masks[("regions", "region")].kind == "mask_hash"


def test_exempt_role_skips_policy():
    p = compute(column_rules=[col_rule("email", "deny", exempt=[ANALYST_ROLE])])
    assert "email" in p.allowed_columns["sales"]


def test_owner_is_not_implicitly_exempt():
    owner = subject(role_id=OWNER_ROLE, role_key="owner")
    p = compute(owner, column_rules=[col_rule("email", "deny")])
    assert "email" not in p.allowed_columns["sales"]
    p2 = compute(owner, column_rules=[col_rule("email", "deny", exempt=[OWNER_ROLE])])
    assert "email" in p2.allowed_columns["sales"]


# --- row policies -------------------------------------------------------------------------------------------------
def test_literal_row_predicate_applies_to_tables_having_the_columns():
    p = compute(row_rules=[row_rule([{"col": "amount", "op": ">=", "value": 10}])])
    assert p.row_predicates["sales"] == RowPredicate(conditions=(("amount", ">=", 10),))
    assert "regions" not in p.row_predicates


def test_user_attribute_binding_single_and_multi_values():
    rules = [row_rule([{"col": "region", "op": "=", "value": "$user.regions"}])]
    single = compute(subject(attributes={"regions": ("north",)}), row_rules=rules)
    assert single.row_predicates["sales"].conditions == (("region", "=", "north"),)
    assert single.row_predicates["regions"].conditions == (("region", "=", "north"),)
    multi = compute(subject(attributes={"regions": ("north", "south")}), row_rules=rules)
    assert multi.row_predicates["sales"].conditions == (("region", "in", ("north", "south")),)


def test_user_attribute_with_in_and_not_equal():
    p = compute(subject(attributes={"regions": ("east", "west")}), row_rules=[
        row_rule([{"col": "region", "op": "in", "value": "$user.regions"}]),
        row_rule([{"col": "region", "op": "!=", "value": "$user.regions"}], rid="z-second"),
    ])
    conds = p.row_predicates["sales"].conditions
    assert ("region", "in", ("east", "west")) in conds
    assert ("region", "not_in", ("east", "west")) in conds


def test_builtin_user_attributes():
    p = compute(row_rules=[row_rule([
        {"col": "email", "op": "=", "value": "$user.email"},
        {"col": "order_id", "op": "!=", "value": "$user.id"},
    ])])
    assert p.row_predicates["sales"].conditions == (("email", "=", "ana@example.test"), ("order_id", "!=", str(USER)))


def test_missing_attribute_fails_closed():
    p = compute(subject(attributes={}), row_rules=[row_rule([{"col": "region", "op": "in", "value": "$user.regions"}])])
    for t in ("sales", "regions"):
        (cond,) = p.row_predicates[t].conditions
        assert cond[1] == "in" and cond[2] == ()


def test_range_comparison_against_multi_valued_attribute_fails_closed():
    p = compute(subject(attributes={"limit": ("5", "9")}),
                row_rules=[row_rule([{"col": "amount", "op": "<", "value": "$user.limit"}])])
    (cond,) = p.row_predicates["sales"].conditions
    assert cond[1:] == ("in", ())


def test_policies_are_anded_and_deduplicated():
    p = compute(row_rules=[
        row_rule([{"col": "amount", "op": ">", "value": 0}], rid="a"),
        row_rule([{"col": "region", "op": "in", "value": ["north"]}, {"col": "amount", "op": ">", "value": 0}],
                 rid="b"),
    ])
    assert p.row_predicates["sales"].conditions == (("amount", ">", 0), ("region", "in", ("north",)))


def test_partially_enforceable_predicate_denies_that_table():
    # regions has "region" but not "amount": the policy cannot be fully enforced there -> no rows
    p = compute(row_rules=[row_rule([{"col": "region", "op": "=", "value": "x"}, {"col": "amount", "op": ">", "value": 1}])])
    assert p.row_predicates["sales"].conditions == (("region", "=", "x"), ("amount", ">", 1))
    (deny,) = p.row_predicates["regions"].conditions
    assert deny[1:] == ("in", ())


def test_policy_on_unknown_column_denies_every_table():
    p = compute(row_rules=[row_rule([{"col": "tenant_code", "op": "=", "value": "A"}])])
    assert all(pred.conditions and pred.conditions[0][1:] == ("in", ()) for pred in p.row_predicates.values())
    assert set(p.row_predicates) == {"sales", "regions"}


def test_corrupt_stored_predicate_fails_closed():
    bad = RowRule(id="x", principal_type="everyone", principal_id=None, predicate={"any": []})
    p = compute(row_rules=[bad])
    assert set(p.row_predicates) == {"sales", "regions"}


def test_inactive_exempt_and_foreign_row_rules_are_ignored():
    p = compute(row_rules=[
        row_rule([{"col": "amount", "op": ">", "value": 1}], active=False),
        row_rule([{"col": "amount", "op": ">", "value": 2}], exempt=[ANALYST_ROLE]),
        row_rule([{"col": "amount", "op": ">", "value": 3}], ptype="user", pid=OTHER_USER),
        row_rule([{"col": "amount", "op": ">", "value": 4}], ptype="role", pid=OWNER_ROLE),
    ])
    assert p.row_predicates == {}


def test_null_operators():
    p = compute(row_rules=[row_rule([{"col": "email", "op": "not_null"}])])
    assert p.row_predicates["sales"].conditions == (("email", "not_null", None),)


# --- fingerprint ----------------------------------------------------------------------------------------------------
def test_fingerprint_is_stable_and_order_independent():
    r1 = col_rule("email", "deny", rid="1")
    r2 = col_rule("phone", "mask_hash", rid="2")
    rows = [row_rule([{"col": "amount", "op": ">", "value": 0}], rid="r")]
    a = compute(column_rules=[r1, r2], row_rules=rows)
    b = compute(column_rules=[r2, r1], row_rules=rows,
                columns={"sales": list(reversed(COLUMNS["sales"])), "regions": COLUMNS["regions"]})
    assert a.fingerprint == b.fingerprint
    assert a == b


def test_fingerprint_changes_with_any_input():
    base = compute(subject(attributes={"regions": ("north",)})).fingerprint
    assert compute(subject(attributes={"regions": ("south",)})).fingerprint != base
    assert compute(subject(attributes={"regions": ("north",)}), column_rules=[col_rule("email", "deny", rid="1")]).fingerprint != base
    assert compute(subject(attributes={"regions": ("north",)}), row_cap=10).fingerprint != base
    assert compute(subject(attributes={"regions": ("north",)}, user_id=OTHER_USER)).fingerprint != base


# --- predicate validation ---------------------------------------------------------------------------------------------
def test_validate_predicate_accepts_valid_shapes():
    pred = {"all": [
        {"col": "region", "op": "in", "value": ["north", "south"]},
        {"col": "amount", "op": ">=", "value": 10.5},
        {"col": "owner", "op": "=", "value": "$user.id"},
        {"col": "email", "op": "is_null"},
    ]}
    out = validate_predicate(pred)
    assert out["all"][3] == {"col": "email", "op": "is_null", "value": None}


@pytest.mark.parametrize(
    "pred",
    [
        None,
        [],
        {"any": []},
        {"all": []},
        {"all": "x"},
        {"all": [{"col": "a", "op": "like", "value": "%x%"}]},
        {"all": [{"col": "a", "op": "in", "value": "north"}]},
        {"all": [{"col": "a", "op": "in", "value": ["$user.regions"]}]},
        {"all": [{"col": "a", "op": "=", "value": {"x": 1}}]},
        {"all": [{"col": "a", "op": "=", "value": ["x"]}]},
        {"all": [{"col": "a", "op": "is_null", "value": 1}]},
        {"all": [{"col": "a", "op": "=", "value": "$user.Bad-Name"}]},
        {"all": [{"col": 'a"; drop', "op": "=", "value": 1}]},
        {"all": [{"col": "a", "op": "=", "value": 1, "extra": True}]},
        {"all": [{"col": "a", "op": "=", "value": 1}] * 21},
    ],
)
def test_validate_predicate_rejects_invalid(pred):
    with pytest.raises(ValueError):
        validate_predicate(pred)
