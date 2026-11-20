"""W2 contract alignment: reject-code vocabulary shared with sqlguard + DatasetSemantics cache scoping."""

from __future__ import annotations

from uuid import UUID

import pytest

from apps.nl2sql import cache, services, sqlutil
from apps.nl2sql.sqlutil import parse_select, validate_against_context
from nexora.contracts import GUARD_REJECT_CODES, REPAIRABLE_REJECT_CODES, DatasetSemantics

from .conftest import DATASET, METRICS, SALES_COLUMNS, make_ctx

VERSION = UUID("0190a0c4-0000-7000-8000-00000000e001")


def test_repairable_codes_are_the_contract_codes():
    assert sqlutil.REPAIRABLE_CODES is REPAIRABLE_REJECT_CODES or sqlutil.REPAIRABLE_CODES == REPAIRABLE_REJECT_CODES
    assert sqlutil.SECURITY_CODES == GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES
    assert not (sqlutil.SECURITY_CODES & sqlutil.REPAIRABLE_CODES)


@pytest.mark.parametrize("sql", [
    "", "SELECT FROM WHERE (", "SELECT 1; SELECT 2", "DELETE FROM sales", "SELECT * INTO evil FROM sales",
    "SELECT * FROM sales FOR UPDATE", "SELECT * FROM sales WHERE id = :p", "VACUUM",
])
def test_local_parse_codes_use_guard_vocabulary(sql):
    _ast, issue = parse_select(sql)
    assert issue is not None and issue.code in GUARD_REJECT_CODES


@pytest.mark.parametrize("sql", [
    "SELECT customer_secret FROM sales", "SELECT * FROM customers", "SELECT * FROM pg_catalog.pg_user",
    "SELECT * FROM information_schema.tables", "SELECT ctid FROM sales", "SELECT * FROM other.sales",
])
def test_local_context_codes_use_guard_vocabulary(sql):
    ast, issue = parse_select(sql)
    assert issue is None
    found = validate_against_context(ast, {"sales": "org_x.ds"}, {"sales": frozenset({"region", "amount"})})
    assert found is not None and found.code in GUARD_REJECT_CODES


@pytest.mark.parametrize("code", sorted(GUARD_REJECT_CODES - REPAIRABLE_REJECT_CODES))
def test_every_security_code_is_never_repaired(code):
    out = services.repair("revenue by region", "SELECT 1", code, make_ctx())
    assert out.sql is None and out.blocked_reason == "not_repairable"


def _semantics(dataset_id=DATASET, version=VERSION) -> DatasetSemantics:
    return DatasetSemantics(
        dataset_id=dataset_id, dataset_version_id=version, dataset_name="Synthetic Sales", classification="synthetic",
        tables={"sales": "org_test.ds_1_v1"},
        columns_by_table={"sales": tuple(c["name"] for c in SALES_COLUMNS)},
        columns_meta={"sales": SALES_COLUMNS}, metrics=METRICS, schema_hash="abc",
    )


def test_resolve_version_prefers_semantics_and_checks_dataset():
    ctx = make_ctx()
    assert services.resolve_version(ctx) is None
    assert services.resolve_version(ctx, semantics=_semantics()) == VERSION
    assert services.resolve_version(ctx, dataset_version_id=VERSION, semantics=_semantics()) == VERSION
    with pytest.raises(ValueError):
        services.resolve_version(ctx, semantics=_semantics(dataset_id=UUID(int=5)))
    with pytest.raises(ValueError):
        services.resolve_version(ctx, dataset_version_id=UUID(int=9), semantics=_semantics())
    assert cache.scope_id(ctx, services.resolve_version(ctx, semantics=_semantics())) == VERSION
    assert cache.scope_id(ctx, None) == DATASET


def test_propose_passes_semantics_version_to_cache(monkeypatch):
    seen = []
    monkeypatch.setattr(cache, "lookup_exact", lambda ctx, slots, dataset_version_id=None: seen.append(
        ("exact", dataset_version_id)))
    monkeypatch.setattr(cache, "lookup_semantic", lambda ctx, slots, dataset_version_id=None: seen.append(
        ("semantic", dataset_version_id)))
    out = services.propose("total revenue by region", make_ctx(), semantics=_semantics())
    assert out.source == "builder"
    assert seen == [("exact", VERSION), ("semantic", VERSION)]


def test_context_from_semantics_filters_to_allowed_columns():
    base = make_ctx(allowed={"region", "amount", "order_date", "order_id"})
    ctx = services.context_from_semantics(_semantics(), base.policy, user_id=base.user_id)
    names = {c["name"] for c in ctx.columns_meta["sales"]}
    assert names == {"region", "amount", "order_date", "order_id"}
    assert "customer_email" not in names
    # gross_margin mentions `discount`, which is not allowed -> dropped; aov uses only allowed columns
    assert [m["key"] for m in ctx.metrics] == ["aov"]
    assert ctx.dataset_classification == "synthetic" and ctx.dataset_id == DATASET


def test_context_from_semantics_rejects_mismatched_policy():
    base = make_ctx()
    with pytest.raises(ValueError):
        services.context_from_semantics(_semantics(dataset_id=UUID(int=7)), base.policy, user_id=base.user_id)
