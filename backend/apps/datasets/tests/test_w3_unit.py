"""W3 datasets (pure, no DB): notification helper, staged type casting equals a single pass, storage-agnostic errors."""

from __future__ import annotations

import contextlib
import sys
import types
import uuid

import polars as pl
import pytest

from apps.datasets import notifications as N
from apps.datasets.ingest import inference as I


# ------------------------------------------------------------------------------------------------ notifications
@pytest.fixture
def fake_notify(monkeypatch):
    calls = []
    mod = types.ModuleType("apps.core.notifications")
    mod.notify = lambda user_ids, kind, title, body, link: calls.append((user_ids, kind, title, body, link))
    monkeypatch.setitem(sys.modules, "apps.core.notifications", mod)
    monkeypatch.setattr(N.transaction, "atomic", contextlib.nullcontext)
    return calls


def test_recipients_are_distinct_valid_uuids():
    a = uuid.uuid4()
    assert N.recipients(a, str(a), None, "", "not-a-uuid", uuid.UUID(int=5)) == [a, uuid.UUID(int=5)]


def test_notify_users_calls_the_core_service(fake_notify):
    u = uuid.uuid4()
    assert N.notify_users([u, None, str(u)], N.DATASET_READY, "T" * 300, "B" * 2000, "/o/acme/data/datasets/1")
    (ids, kind, title, body, link), = fake_notify
    assert ids == [u] and kind == "dataset.ingest_finished" and len(title) == N.TITLE_MAX and len(body) == N.BODY_MAX
    assert link == "/o/acme/data/datasets/1"


def test_notify_users_without_recipients_is_a_noop(fake_notify):
    assert N.notify_users([None, ""], N.DATASET_FAILED, "t") is False and fake_notify == []


def test_notify_users_skips_when_the_service_is_missing(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "apps.core.notifications", None)
    import logging

    with caplog.at_level(logging.INFO, logger="apps.datasets.notifications"):
        assert N.notify_users([uuid.uuid4()], N.IMPORT_FAILED, "t") is False
    assert "not installed" in caplog.text


def test_notify_users_never_raises(monkeypatch):
    mod = types.ModuleType("apps.core.notifications")

    def broken(*a):
        raise RuntimeError("db down")

    mod.notify = broken
    monkeypatch.setitem(sys.modules, "apps.core.notifications", mod)
    monkeypatch.setattr(N.transaction, "atomic", contextlib.nullcontext)
    assert N.notify_users([uuid.uuid4()], N.PIPELINE_RUN_FAILED, "t") is False


def test_kinds_are_stable_short_strings():
    kinds = [N.DATASET_READY, N.DATASET_FAILED, N.IMPORT_FINISHED, N.IMPORT_FAILED, N.PIPELINE_RUN_SUCCEEDED,
             N.PIPELINE_RUN_FAILED]
    assert len(set(kinds)) == 6 and all(len(k) <= 32 for k in kinds)


def test_services_reexport_the_helpers():
    from apps.datasets import services

    assert services.notify_users is N.notify_users and services.org_link is N.org_link


def test_services_have_no_direct_boto_dependency():
    import inspect

    from apps.datasets import services

    assert "botocore" not in inspect.getsource(services)


# ------------------------------------------------------------------------------------------------ staged casting
def _single_pass(raw: pl.Series, plan: I.TypePlan) -> pl.Series:
    cleaned = I.clean_series(raw)
    return I._eval(cleaned, I.parse_expr(pl.col("v"), plan))


def test_staged_date_cast_equals_single_pass_including_formats_unseen_in_the_sample():
    values = ["2024-01-05"] * 200 + ["05/02/2024", "7 Mar 2024", "bad", None, " 2024-12-31 "]
    raw = pl.Series("d", values, dtype=pl.String)
    formats = I.ISO_DATE_FORMATS + I.DMY_DATE_FORMATS
    plan = I.TypePlan("date", formats=formats, primary_formats=("%Y-%m-%d",))
    staged = I.cast_column(raw, plan)
    assert staged.series.to_list() == _single_pass(raw, plan).to_list()
    assert staged.invalid_count == 1  # only "bad"


def test_staged_cast_keeps_the_first_match_order_for_formats_unseen_in_the_sample():
    """'05/02/2024' parses as d/m/Y AND m/d/Y. The sample only hit m/d/Y (index 1), but d/m/Y comes first in the
    family, so a single pass reads it as 5 Feb. The staged first pass is a prefix of the family and agrees."""
    formats = ("%d/%m/%Y", "%m/%d/%Y")
    sample = pl.Series("v", ["12/25/2024", "11/30/2024"], dtype=pl.String)
    primary = I._hit_formats(sample, formats, I.date_expr)
    assert primary == formats  # prefix up to the last hit, not just the hit
    plan = I.TypePlan("date", formats=formats, primary_formats=primary)
    raw = pl.Series("d", ["12/25/2024", "05/02/2024"], dtype=pl.String)
    staged = I.cast_column(raw, plan).series.to_list()
    assert staged == _single_pass(raw, plan).to_list()
    assert str(staged[1]) == "2024-02-05"


def test_staged_bigint_cast_equals_single_pass():
    raw = pl.Series("q", ["1", "+2", "1,234", "12.0", "12.5", "x", "", None, "007"], dtype=pl.String)
    plan = I.TypePlan("bigint")
    assert I.cast_column(raw, plan).series.to_list() == _single_pass(raw, plan).to_list()


def test_inferred_plan_records_the_formats_seen_in_the_sample():
    sample = pl.Series("v", [f"2024-01-0{i % 9 + 1}" for i in range(50)], dtype=pl.String)
    plan = I.infer_plan(sample)
    assert plan.pg_type == "date" and plan.primary_formats == ("%Y-%m-%d",)
    assert set(plan.primary_formats) <= set(plan.formats)


def test_early_exit_is_exact():
    words = pl.Series("v", ["north"] * 5000, dtype=pl.String)
    assert I._cannot_reach(words, I.int_expr(pl.col("v")), I.PARSE_THRESHOLD) is True
    # a column whose head is clean is never pruned
    ints = pl.Series("v", [str(i) for i in range(5000)], dtype=pl.String)
    assert I._cannot_reach(ints, I.int_expr(pl.col("v")), I.PARSE_THRESHOLD) is False
    # small samples are always evaluated in full
    assert I._cannot_reach(words.head(100), I.int_expr(pl.col("v")), I.PARSE_THRESHOLD) is False


def test_large_text_column_still_infers_text_and_numbers_still_infer_numbers():
    n = 20_000
    text = pl.Series("v", [f"SKU-{i}" for i in range(n)], dtype=pl.String)
    assert I.infer_plan(text).pg_type == "text"
    money = pl.Series("v", [f"₹{i * 1000 + 50:,}.50" for i in range(1, n)], dtype=pl.String)
    assert I.infer_plan(money).pg_type == "numeric"
