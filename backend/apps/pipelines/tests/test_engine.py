"""Engine (pure): step metrics, failure codes, retry policy. IO is faked (no DB, no storage)."""

import uuid

import polars as pl
import pytest

from apps.connectors.exceptions import ConnectionFailed
from apps.pipelines import engine
from apps.pipelines.dag import validate_dag

DS = str(uuid.uuid4())


def dag(*steps):
    nodes = [{"id": "src", "type": "source.dataset", "config": {"dataset_id": DS}}, *steps,
             {"id": "dst", "type": "destination.dataset", "config": {"name": "Out"}}]
    return {"nodes": nodes, "edges": [[a["id"], b["id"]] for a, b in zip(nodes, nodes[1:])]}


FRAME = pl.DataFrame({"id": [1, 2, 2, 3], "city": [" a", "b ", "b ", None], "v": ["1", "x", "x", "3"]})


def io(frame=FRAME, seen=None, fail=None):
    def load_source(node, limit):
        if fail:
            raise fail
        return frame.head(limit) if limit else frame

    def on_step(metric, metrics):
        if seen is not None:
            seen.append((metric.id, metric.status))

    return engine.RunIO(load_source=load_source, load_dataset=lambda i, lim: frame, on_step=on_step)


def test_execute_records_per_step_metrics():
    seen = []
    validation = validate_dag(dag(
        {"id": "t", "type": "trim_whitespace", "config": {}},
        {"id": "dd", "type": "deduplicate", "config": {}},
        {"id": "c", "type": "cast", "config": {"columns": {"v": "integer"}, "on_error": "reject"}},
    ))
    assert validation.ok
    result = engine.execute(validation, io(seen=seen))
    by = {m.id: m for m in result.steps}
    assert result.rows_in == 4 and result.rows_out == 2 and result.rows_rejected == 1
    assert (by["src"].rows_out, by["t"].rows_in, by["dd"].rows_out, by["c"].rows_rejected) == (4, 4, 3, 1)
    assert all(by[k].status == "succeeded" and by[k].duration_ms is not None for k in ("src", "t", "dd", "c"))
    assert by["dst"].status == "pending"  # written by the runner
    assert seen[:2] == [("src", "running"), ("src", "succeeded")]


def test_row_limit_for_preview():
    validation = validate_dag(dag())
    assert engine.execute(validation, io(), row_limit=2).rows_out == 2


def test_step_failure_marks_step_and_skips_rest():
    validation = validate_dag(dag(
        {"id": "c", "type": "cast", "config": {"columns": {"v": "integer"}, "on_error": "fail"}},
        {"id": "t", "type": "trim_whitespace", "config": {}},
    ))
    with pytest.raises(engine.RunFailure) as e:
        engine.execute(validation, io())
    assert e.value.code == "cast_failed" and e.value.node == "c" and e.value.retryable is False


def test_connector_errors_keep_their_code_and_retryability():
    validation = validate_dag(dag())
    with pytest.raises(engine.RunFailure) as e:
        engine.execute(validation, io(fail=ConnectionFailed()))
    assert e.value.code == "connector_connection_failed" and e.value.retryable is True


def test_unexpected_errors_are_not_leaked():
    validation = validate_dag(dag())
    with pytest.raises(engine.RunFailure) as e:
        engine.execute(validation, io(fail=RuntimeError("secret internal detail")))
    assert e.value.code == "step_failed" and "secret" not in e.value.message


def test_max_rows_guard():
    validation = validate_dag(dag())
    with pytest.raises(engine.RunFailure) as e:
        engine.execute(validation, io(), max_rows=3)
    assert e.value.code == "too_many_rows"


def test_retry_policy():
    transient = engine.RunFailure("connector_timeout", "slow")
    permanent = engine.RunFailure("quality_blocking_failure", "bad data")
    assert transient.retryable and not permanent.retryable
    assert engine.should_retry(transient, attempt=1, max_retries=2)
    assert engine.should_retry(transient, attempt=2, max_retries=2)
    assert not engine.should_retry(transient, attempt=3, max_retries=2)
    assert not engine.should_retry(permanent, attempt=1, max_retries=5)
    assert [engine.retry_delay_s(a, 60) for a in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert engine.retry_delay_s(30, 60) == 3600  # capped
    assert engine.retry_delay_s(1, 60, jitter=0.1) == 66
