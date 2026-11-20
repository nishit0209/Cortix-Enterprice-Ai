"""Connector plugin interface (DATA_ENGINEERING_DESIGN §2) and registry.

A connector type registers a subclass of `BaseConnector` with `register_connector`. Adding a connector type never
touches pipelines or the API: they look connectors up by `DataSource.type`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from .exceptions import UnsupportedConnector


@dataclass(frozen=True)
class ReadonlyCheck:
    ok: bool
    problems: tuple[str, ...] = ()            # machine-readable problem keys, e.g. "superuser", "write:public.orders"

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": list(self.problems)}


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest test class

    ok: bool
    server_version: str = ""
    current_user: str = ""
    database: str = ""
    resolved_ip: str = ""
    readonly: ReadonlyCheck = field(default_factory=lambda: ReadonlyCheck(ok=False))
    latency_ms: int = 0
    code: str | None = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["readonly"] = self.readonly.as_dict()
        return d


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True
    ordinal: int = 0


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    kind: str = "table"                       # table | view | materialized_view | foreign_table | partitioned_table
    estimated_rows: int | None = None
    columns: tuple[ColumnInfo, ...] = ()


@dataclass(frozen=True)
class CursorState:
    column: str
    value: Any


@dataclass(frozen=True)
class Batch:
    columns: tuple[ColumnInfo, ...]
    rows: list[tuple]


class BaseConnector(ABC):
    """One configured connection to an external system. Never logs or returns the secret."""

    type: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    @abstractmethod
    def test(self) -> TestResult: ...

    @abstractmethod
    def introspect(self) -> list[TableInfo]: ...

    @abstractmethod
    def read_batches(self, table: str, columns: Sequence[str] | None = None, batch_rows: int = 10_000, *,
                     schema: str | None = None, cursor_column: str | None = None, after: Any = None,
                     max_rows: int | None = None) -> Iterator[Batch]: ...

    @abstractmethod
    def incremental_cursor(self, table: str, cursor_column: str, *, schema: str | None = None) -> CursorState: ...


_registry: dict[str, type[BaseConnector]] = {}


def register_connector(cls: type[BaseConnector]) -> type[BaseConnector]:
    _registry[cls.type] = cls
    return cls


def connector_class(type_: str) -> type[BaseConnector]:
    try:
        return _registry[type_]
    except KeyError as exc:
        raise UnsupportedConnector(f"Connector type {type_!r} is not supported.") from exc


def registered_types() -> list[str]:
    return sorted(_registry)
