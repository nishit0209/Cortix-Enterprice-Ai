"""Quality rule validation/evaluation, suggestions, expressions, duplicates and scoring (no DB)."""

import datetime as dt
from uuid import uuid4

import polars as pl
import pytest

from apps.datasets.ingest.inference import parse_as_type
from apps.quality import engine
from apps.quality.duplicates import find_duplicates, fuzzy_clusters, normalize_text
from apps.quality.engine import EvalContext, RuleSpec, RuleValidationError
from apps.quality.expressions import ExpressionError, compile_expression

FRAME = pl.DataFrame({
    "id": [1, 2, 3, 3, None],
    "email": ["a@example.test", "b@example.test", None, "bad-email", "c@example.test"],
    "amount": [10.0, 250.0, -5.0, 40.0, 30.0],
    "status": ["paid", "paid", "refunded", "unknown", "paid"],
    "day": [dt.date(2024, 1, d) for d in (1, 2, 3, 4, 5)],
    "raw": ["1", "2", "x", "4", None],
})
TYPES = {"id": "bigint", "email": "text", "amount": "double precision", "status": "text", "day": "date", "raw": "text"}


def ctx(**kw):
    base = dict(column_types=TYPES, pii_columns={"email"}, type_parser=parse_as_type)
    base.update(kw)
    return EvalContext(**base)


def run(rule_type, col=None, **params):
    return engine.evaluate_rule(FRAME, RuleSpec(rule_type, col, params), ctx())


# --- evaluation ---------------------------------------------------------------------------------------
def test_not_null_and_unique():
    r = run("not_null", "email")
    assert (r.passed, r.evaluated_count, r.failed_count) == (False, 5, 1)
    assert r.sample_failures == [{"row": 3, "value": None}]
    u = run("unique", "id")
    assert (u.passed, u.evaluated_count, u.failed_count) == (False, 4, 2)


def test_composite_unique():
    r = run("unique", None, columns=["id", "status"])
    assert r.failed_count == 0 and r.evaluated_count == 4


def test_range_numeric_and_dates_and_exclusive():
    r = run("range", "amount", min=0, max=100)
    assert r.failed_count == 2 and {s["value"] for s in r.sample_failures} == {250.0, -5.0}
    assert run("range", "amount", min=10, max=250, inclusive=False).failed_count == 3
    d = run("range", "day", min="2024-01-02", max="2024-01-04")
    assert d.failed_count == 2


def test_regex_full_match_and_pii_masking():
    r = run("regex", "email", pattern=r"[^@\s]+@[^@\s]+\.[a-z]+")
    assert r.failed_count == 1 and r.evaluated_count == 4
    assert r.sample_failures[0]["value"].endswith("il") and "bad" not in r.sample_failures[0]["value"]


def test_allowed_values_text_numeric_boolean():
    assert run("allowed_values", "status", values=["paid", "refunded"]).failed_count == 1
    assert run("allowed_values", "amount", values=[10, 40, 30]).failed_count == 2
    frame = pl.DataFrame({"flag": [True, False, None]})
    r = engine.evaluate_rule(frame, RuleSpec("allowed_values", "flag", {"values": ["true"]}),
                             EvalContext(column_types={"flag": "boolean"}))
    assert r.failed_count == 1


def test_type_rule_uses_parser():
    r = run("type", "raw", data_type="bigint")
    assert r.failed_count == 1 and r.evaluated_count == 4
    assert run("type", "amount", data_type="numeric").passed


def test_freshness():
    now = lambda: dt.datetime(2024, 1, 6, 12, tzinfo=dt.UTC)  # noqa: E731
    ok = engine.evaluate_rule(FRAME, RuleSpec("freshness", "day", {"max_age_hours": 48}), ctx(now=now))
    assert ok.passed
    stale = engine.evaluate_rule(FRAME, RuleSpec("freshness", "day", {"max_age_hours": 1}), ctx(now=now))
    assert not stale.passed and stale.sample_failures[0]["age_hours"] > 1


def test_row_count():
    assert run("row_count", min=1, max=10).passed
    assert not run("row_count", min=6).passed


def test_referential():
    ref_id = uuid4()
    lookup = lambda dataset_id, column: pl.Series(["paid", "refunded"])  # noqa: E731
    r = engine.evaluate_rule(FRAME, RuleSpec("referential", "status", {"dataset_id": str(ref_id), "column": "code"}),
                             ctx(reference_values=lookup))
    assert r.failed_count == 1
    no_lookup = run("referential", "status", dataset_id=str(ref_id), column="code")
    assert not no_lookup.passed and no_lookup.error


def test_expression_rule_null_passes():
    r = run("expression", expression="amount >= 0 AND status IN ('paid', 'refunded', 'unknown')")
    assert r.failed_count == 1  # amount -5
    r2 = run("expression", expression="id > 1")
    assert r2.failed_count == 1  # id=1 fails, NULL id passes (SQL CHECK semantics)


def test_evaluation_errors_are_captured_not_raised():
    r = run("not_null", "missing_column")
    assert not r.passed and "column" in r.error
    assert r.pass_rate == 0.0


# --- validation ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spec,field",
    [
        (RuleSpec("not_null", None), "column"),
        (RuleSpec("not_null", "nope"), "column"),
        (RuleSpec("range", "amount", {}), "params"),
        (RuleSpec("range", "amount", {"min": 5, "max": 1}), "params"),
        (RuleSpec("range", "status", {"min": 1}), "params"),
        (RuleSpec("regex", "status", {"pattern": "("}), "params.pattern"),
        (RuleSpec("regex", "status", {"pattern": "a" * 600}), "params.pattern"),
        (RuleSpec("allowed_values", "status", {"values": []}), "params.values"),
        (RuleSpec("allowed_values", "status", {"values": [{"x": 1}]}), "params.values"),
        (RuleSpec("type", "raw", {"data_type": "varchar"}), "params.data_type"),
        (RuleSpec("freshness", "status", {"max_age_hours": 5}), "column"),
        (RuleSpec("freshness", "day", {"max_age_hours": -1}), "params.max_age_hours"),
        (RuleSpec("row_count", None, {"min": -1}), "params.min"),
        (RuleSpec("referential", "status", {"dataset_id": "nope", "column": "x"}), "params.dataset_id"),
        (RuleSpec("expression", None, {"expression": "unknown_col > 1"}), "params.expression"),
        (RuleSpec("not_null", "id", {}, severity="fatal"), "severity"),
        (RuleSpec("bogus", "id"), "rule_type"),
    ],
)
def test_validation_errors(spec, field):
    with pytest.raises(RuleValidationError) as exc:
        engine.validate_rule(spec, TYPES)
    assert field in exc.value.errors


def test_validation_normalizes_params():
    assert engine.validate_rule(RuleSpec("range", "amount", {"min": 1}), TYPES) == {"min": 1, "max": None, "inclusive": True}
    assert engine.validate_rule(RuleSpec("referential", "status", {"dataset_id": str(uuid4()).upper(), "column": "c"}), TYPES)[
        "dataset_id"].islower()


# --- expressions ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "amount > 0", "NOT (amount < 0)", "amount BETWEEN 0 AND 100", "lower(status) LIKE 'pa%'",
        "status ILIKE 'PA_D'", "coalesce(id, 0) <> 1", "length(trim(status)) > 2", "abs(amount) < 300",
        "-amount + 2 * amount >= 0", "id IS NOT NULL OR email IS NULL", "status NOT IN ('x')", "amount % 2 = 0",
    ],
)
def test_expression_subset_compiles_and_runs(text):
    expr, used = compile_expression(text, FRAME.columns)
    assert used
    FRAME.select(expr)


@pytest.mark.parametrize(
    "text",
    [
        "", "amount > (SELECT 1)", "pg_sleep(1) IS NULL", "amount > 0; DROP TABLE x", "other.amount > 1",
        "upper(status) SIMILAR TO 'x'", "status IN (lower('a'))", "x" * 1001, "random() > 0.5",
    ],
)
def test_expression_rejects_unsafe_or_unknown(text):
    with pytest.raises(ExpressionError):
        compile_expression(text, FRAME.columns)


def test_like_semantics():
    expr, _ = compile_expression("status LIKE 'p_id'", ["status"])
    assert pl.DataFrame({"status": ["paid", "pid", "xpaid"]}).select(expr).to_series().to_list() == [True, False, False]


# --- suggestions --------------------------------------------------------------------------------------
def test_suggest_rules_from_profile():
    columns = [
        {"name": "customer_id", "data_type": "bigint", "semantic_type": "id", "is_pii": False,
         "stats": {"null_count": 0, "distinct": 100, "p1": 1, "p99": 99, "min": 1, "top_values": []}},
        {"name": "segment", "data_type": "text", "semantic_type": "category", "is_pii": False,
         "stats": {"null_count": 3, "distinct": 3, "top_values": [{"value": "gold", "count": 50},
                                                                 {"value": "silver", "count": 30},
                                                                 {"value": "bronze", "count": 17}]}},
        {"name": "email", "data_type": "text", "semantic_type": "email", "is_pii": True,
         "stats": {"null_count": 0, "distinct": 100, "top_values": None}},
        {"name": "payload", "data_type": "jsonb", "semantic_type": "json", "stats": {"null_count": 0}},
    ]
    rules = engine.suggest_rules(columns, {"row_count": 100, "candidate_keys": ["customer_id", "email"]})
    kinds = {(r.rule_type, r.column) for r in rules}
    assert ("not_null", "customer_id") in kinds and ("unique", "customer_id") in kinds
    assert ("range", "customer_id") in kinds
    assert ("allowed_values", "segment") in kinds and ("not_null", "segment") not in kinds
    assert ("unique", "email") in kinds and ("allowed_values", "email") not in kinds
    assert not any(r.column == "payload" for r in rules)
    rng = next(r for r in rules if r.rule_type == "range")
    assert rng.params["min"] == 0 and rng.params["max"] >= 99
    assert engine.suggest_rules(columns, {"row_count": 0}) == []


# --- duplicates & scoring -------------------------------------------------------------------------------
def test_normalize_text():
    assert normalize_text("  New-Delhi.  ") == "new delhi"


def test_fuzzy_clusters_with_blocking():
    values = ["Mumbai"] * 10 + ["mumbai "] * 2 + ["Mumbay"] * 1 + ["Pune"] * 5 + ["Delhi"] * 4 + ["Chennai"] * 3
    out = fuzzy_clusters(pl.Series(values))
    assert len(out["clusters"]) == 1
    cluster = out["clusters"][0]
    assert cluster["representative"] == "Mumbai" and cluster["distinct_values"] == 3 and cluster["rows"] == 13
    assert out["inconsistent_rows"] == 3 and out["checked_rows"] == len(values)


def test_fuzzy_clusters_masked():
    out = fuzzy_clusters(pl.Series(["Asha Rao", "asha rao"]), mask_values=True)
    assert out["clusters"][0]["representative"] == "***"


def test_find_duplicates_exact_key_and_fuzzy():
    frame = pl.DataFrame({
        "order_id": [1, 2, 2, 3, 4, 4],
        "city": ["Pune", "Mumbai", "Mumbai", "mumbai", "Delhi", "Delhi"],
        "email": ["a@x.test", "b@x.test", "b@x.test", "c@x.test", "d@x.test", "d@x.test"],
    })
    columns = [
        {"name": "order_id", "data_type": "bigint", "semantic_type": "id", "is_pii": False, "stats": {"distinct": 4}},
        {"name": "city", "data_type": "text", "semantic_type": "geo_city", "is_pii": False,
         "stats": {"distinct": 4, "max_length": 6}},
        {"name": "email", "data_type": "text", "semantic_type": "email", "is_pii": True, "stats": {"distinct": 4}},
    ]
    dup = find_duplicates(frame, columns, key_columns=["order_id", "email"])
    assert dup["exact_rows"] == 2
    assert dup["key_duplicates"] == 4
    email_key = next(k for k in dup["key_columns"] if k["column"] == "email")
    assert all(s["value"] == "***" for s in email_key["sample"])
    assert dup["fuzzy_clusters"][0]["column"] == "city"
    assert dup["inconsistent_rows"] == 1


def test_dimensions_and_score():
    frame = pl.DataFrame({"a": [1, None, 3, 4], "b": ["x", "y", "y", "z"]})
    ok = engine.RuleOutcome(passed=True, evaluated_count=4, failed_count=0)
    bad = engine.RuleOutcome(passed=False, evaluated_count=4, failed_count=2)
    outcomes = [(RuleSpec("not_null", "a", severity="error"), bad), (RuleSpec("range", "a", severity="warn"), ok),
                (RuleSpec("regex", "b", severity="info"), bad)]
    dims = engine.compute_dimensions(frame, {"a": "bigint", "b": "text"}, outcomes,
                                     {"exact_rows": 0, "key_columns": [], "inconsistent_rows": 1,
                                      "consistency_checked_rows": 4}, invalid_cells=1)
    assert dims["completeness"] == 87.5
    assert dims["uniqueness"] == 100.0
    assert dims["consistency"] == 75.0
    assert 0 < dims["validity"] < 100
    score = engine.compute_score(dims, outcomes)
    rule_score = 100 * (3 * 0.5 + 1 * 1.0) / 4
    dim_mean = sum(dims[k] for k in ("completeness", "uniqueness", "validity", "consistency")) / 4
    assert score == pytest.approx(round(0.5 * rule_score + 0.5 * dim_mean, 2))
    assert engine.compute_score(dims, []) == pytest.approx(round(dim_mean, 2))
    assert 0 <= score <= 100
