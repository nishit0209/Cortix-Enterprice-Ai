"""Executes a validated DAG over Polars frames, recording per-step metrics. IO is injected (pure & testable)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

import polars as pl
from django.utils import timezone

from . import steps as S
from .dag import DagValidation, Node

RETRYABLE_CODES = frozenset({
    "connector_connection_failed", "connector_timeout", "connector_host_unresolvable", "storage_unavailable",
    "warehouse_unavailable", "query_timeout", "worker_lost", "transient_error", "ingest_timeout",
})


class RunFailure(Exception):
    """A run failed with a stable code. `retryable` decides whether the runner schedules another attempt."""

    def __init__(self, code: str, message: str, *, node: str | None = None, retryable: bool | None = None,
                 detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.node = node
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.detail = detail or {}


@dataclass
class StepMetric:
    id: str
    type: str
    status: str = "pending"              # pending | running | succeeded | failed | skipped
    rows_in: int | None = None
    rows_out: int | None = None
    rows_rejected: int = 0
    duration_ms: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunIO:
    load_source: Callable[[Node, int | None], pl.DataFrame]
    load_dataset: Callable[[UUID, int | None], pl.DataFrame] | None = None
    on_step: Callable[[StepMetric, list[StepMetric]], None] | None = None


@dataclass
class ExecutionResult:
    frame: pl.DataFrame
    steps: list[StepMetric]
    rows_in: int
    rows_out: int
    rows_rejected: int
    inputs: list[dict[str, Any]] = field(default_factory=list)


def _error_of(exc: BaseException) -> tuple[str, str, bool | None]:
    if isinstance(exc, RunFailure):
        return exc.code, exc.message, exc.retryable
    if isinstance(exc, S.StepRuntimeError):
        return exc.code, str(exc), exc.retryable
    codes = getattr(exc, "get_codes", None)
    if callable(codes):  # NexoraError subclasses (connector / dataset errors)
        code = codes() if isinstance(codes(), str) else "step_failed"
        detail = getattr(exc, "detail", "")
        return code, str(detail), getattr(exc, "retryable", None)
    if isinstance(exc, MemoryError):
        return "resource_exhausted", "The step ran out of memory.", False
    if isinstance(exc, pl.exceptions.PolarsError):
        return "step_failed", f"The step failed: {type(exc).__name__}.", False
    return "step_failed", "The step failed unexpectedly.", False


def _now() -> str:
    return timezone.now().isoformat()


def initial_metrics(validation: DagValidation) -> list[StepMetric]:
    return [StepMetric(id=n.id, type=n.type) for n in validation.order]


def execute(validation: DagValidation, io: RunIO, *, row_limit: int | None = None,
            side_row_limit: int | None = None, max_rows: int | None = None) -> ExecutionResult:
    """Run source + steps (NOT the destination write) and return the output frame with metrics.

    `row_limit` caps the source for previews, `side_row_limit` caps join inputs. `max_rows` fails the run when the
    source is larger.
    """
    metrics = initial_metrics(validation)
    by_id = {m.id: m for m in metrics}
    ctx = S.StepContext(load_dataset=io.load_dataset, row_limit=row_limit)
    if io.load_dataset is not None:
        loader = io.load_dataset
        ctx.load_dataset = lambda ds_id, _limit: loader(ds_id, side_row_limit)

    def notify(m: StepMetric) -> None:
        if io.on_step:
            io.on_step(m, metrics)

    frame: pl.DataFrame | None = None
    rejected_total = 0
    rows_in = 0
    for node in validation.order:
        m = by_id[node.id]
        if node.is_destination:
            continue  # written by the runner (upload path)
        m.status, m.started_at = "running", _now()
        notify(m)
        started = time.monotonic()
        try:
            if node.is_source:
                frame = io.load_source(node, row_limit)
                m.rows_in = None
                rows_in = frame.height
                if max_rows is not None and frame.height > max_rows:
                    raise RunFailure("too_many_rows", f"The source has more than {max_rows} rows.", node=node.id)
                m.rows_out = frame.height
            else:
                assert frame is not None
                m.rows_in = frame.height
                out = S.STEPS[node.type].apply(frame, node.config, ctx)
                frame = out.frame
                m.rows_out = frame.height
                m.rows_rejected = out.rejected
                rejected_total += out.rejected
        except Exception as exc:  # noqa: BLE001 - every failure becomes a stable code on the step + run
            code, message, retryable = _error_of(exc)
            m.status, m.finished_at = "failed", _now()
            m.duration_ms = int((time.monotonic() - started) * 1000)
            m.error = {"code": code, "message": message}
            for later in metrics[metrics.index(m) + 1:]:
                later.status = "skipped"
            notify(m)
            raise RunFailure(code, message, node=node.id, retryable=retryable) from exc
        m.status, m.finished_at = "succeeded", _now()
        m.duration_ms = int((time.monotonic() - started) * 1000)
        notify(m)
    assert frame is not None
    return ExecutionResult(frame=frame, steps=metrics, rows_in=rows_in, rows_out=frame.height,
                           rows_rejected=rejected_total, inputs=list(ctx.inputs))


def retry_delay_s(attempt: int, base_s: int, *, jitter: float = 0.0, cap_s: int = 3600) -> int:
    """Exponential backoff: base * 2^(attempt-1) (+ jitter fraction), capped. attempt is the FAILED attempt (1-based)."""
    delay = base_s * (2 ** max(0, attempt - 1))
    delay = delay * (1 + max(0.0, jitter))
    return int(min(cap_s, max(1, delay)))


def should_retry(failure: RunFailure, attempt: int, max_retries: int) -> bool:
    """`attempt` is the attempt that just failed (1-based); max_retries extra attempts are allowed."""
    return failure.retryable and attempt <= max_retries
