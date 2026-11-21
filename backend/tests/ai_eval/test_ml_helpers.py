"""Self-tests of the ML oracle helpers (testing.ml and the parsers in test_ml_workspace). No DB, no network.

The DB-backed ML suites are only as trustworthy as these helpers: the sentinel pickle must really fire when
unpickled, the leakage fixture must really contain the hazards it claims, and the payload readers must fail
loudly instead of passing on a shape they do not understand.
"""

from __future__ import annotations

import io
import math
import pickle

import pytest

from testing import ml


def test_the_sentinel_pickle_fires_when_unpickled_by_pickle_and_joblib():
    joblib = pytest.importorskip("joblib")
    ml.PWNED.clear()
    blob = ml.sentinel_pickle("self-test")
    assert pickle.loads(blob) == "self-test"
    assert joblib.load(io.BytesIO(blob)) == "self-test"
    assert ml.PWNED == ["self-test", "self-test"]
    ml.PWNED.clear()
    assert ml.sha256_hex(blob) == ml.sha256_hex(ml.sentinel_pickle("self-test"))
    assert ml.sha256_hex(blob) != ml.sha256_hex(ml.sentinel_pickle("other"))


def test_the_leaky_fixture_contains_every_hazard():
    import numpy as np

    rows = ml.leaky_churn_rows(1200)
    assert len(rows) == 1200
    assert len({r["row_no"] for r in rows}) == len(rows), "row_no must be all-unique (ID-like)"
    assert len({r["customer_id"] for r in rows}) == len(rows), "customer_id must be all-unique (ID-like)"
    y = np.array([int(r["churned"]) for r in rows], dtype=float)
    assert np.array_equal(y, np.array([r["churn_flag"] for r in rows], dtype=float)), "churn_flag == target"
    post = np.array([r["churn_score_post"] for r in rows], dtype=float)
    corr = float(np.corrcoef(y, post)[0, 1])
    assert 0.98 < abs(corr) < 1.0, f"churn_score_post must be a NEAR duplicate (|corr| > 0.98), got {corr}"
    assert 0.1 < y.mean() < 0.5, "the target is a real binary class mix"


def test_churn_csv_matches_its_manifest():
    import json

    meta = json.loads((ml.CHURN_CSV.parent / "churn_meta.json").read_text(encoding="utf-8"))
    rows = ml.churn_rows()
    assert len(rows) == meta["rows"]
    rate = sum(int(r["churned"]) for r in rows) / len(rows)
    assert math.isclose(rate, meta["positive_rate"], abs_tol=1e-3)
    assert {"nps", "region", "tenure_months", "monthly_spend_inr"} <= set(rows[0])


def test_to_csv_round_trips():
    import csv

    rows = ml.churn_rows(3)
    back = list(csv.DictReader(io.StringIO(ml.to_csv(rows).decode())))
    assert back == rows


def test_pick_reads_alternatives_and_fails_loudly():
    body = {"experiment": {"id": "e1"}, "versions": [{"version": 1}, {"version": 2}]}
    assert ml.pick(body, "experiment_id", "experiment.id") == "e1"
    assert ml.pick(body, "versions.-1.version") == 2
    assert ml.pick(body, "nope", default=None) is None
    with pytest.raises(AssertionError, match="none of"):
        ml.pick(body, "nope", "also.nope")


def test_registration_parser_accepts_the_documented_shapes():
    assert ml.parse_registration({"model_id": "m", "version": 3}) == ("m", 3)
    assert ml.parse_registration({"model": {"id": "m"}, "version": {"version": 2, "id": "v"}}) == ("m", 2)
    assert ml.parse_registration({"version": {"model_id": "m", "version": 1}}) == ("m", 1)


def test_column_names_accepts_lists_dicts_and_schemas():
    assert ml.column_names(["a", "b"]) == ["a", "b"]
    assert ml.column_names([{"name": "a", "dtype": "int"}, {"column": "b"}]) == ["a", "b"]
    assert ml.column_names({"a": {"dtype": "int"}, "b": {}}) == ["a", "b"]
    assert ml.column_names({"columns": [{"name": "x"}]}) == ["x"]
    assert ml.column_names(None) == []


def test_confusion_reader_and_expansion_reproduce_sklearn():
    from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

    from tests.ai_eval.test_ml_workspace import _expand

    y_true = ["0", "0", "1", "1", "1", "0", "1", "0", "0", "1"]
    y_pred = ["0", "1", "1", "0", "1", "0", "1", "0", "1", "1"]
    cm = confusion_matrix(y_true, y_pred, labels=["0", "1"]).tolist()
    for exp in ({"confusion_matrix": {"labels": [0, 1], "matrix": cm}}, {"metrics": {"test": {"confusion_matrix": cm}}}):
        labels, matrix = ml.confusion(exp)
        t, p = _expand(labels, matrix)
        assert accuracy_score(t, p) == accuracy_score(y_true, y_pred)
        assert math.isclose(f1_score(t, p, average="macro"), f1_score(y_true, y_pred, average="macro"))


def test_residual_parser_accepts_the_documented_shapes():
    from tests.ai_eval.test_ml_workspace import _numeric_leaves, _residuals

    assert _residuals({"residuals": {"y_true": [3, 5], "y_pred": [1, 6]}}) == [2.0, -1.0]
    assert _residuals({"residuals": [{"actual": 3, "predicted": 1}, {"residual": -1}]}) == [2.0, -1.0]
    assert _residuals({"metrics": {"test": {"residuals": [0.5, -0.5]}}}) == [0.5, -0.5]
    assert _residuals({"residuals": {"histogram": {"bins": [1, 2]}}}) is None
    leaves = _numeric_leaves({"test": {"accuracy": 0.9, "duration_s": 3.2, "cm": [[1, 2]]}, "trained_at": 5})
    assert leaves == {"test.accuracy": 0.9, "test.cm.0.0": 1.0, "test.cm.0.1": 2.0}


def test_metric_and_holdout_readers():
    exp = {"metrics": {"train": {"accuracy": 1.0}, "test": {"accuracy": 0.8, "RMSE": 2.0}}}
    test = ml.holdout_metrics(exp)
    assert ml.metric(test, "accuracy") == 0.8
    assert ml.metric(test, "rmse", "RMSE") == 2.0
    with pytest.raises(AssertionError):
        ml.metric(test, "f1")
