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
        d["checks"] = self.checks()
        return d

    def checks(self) -> list[dict[str, Any]]:
        """The test as ordered UI checks `[{key, ok, message}]` (dns -> ssrf -> tls -> login -> readonly).

        Checks run in order and stop at the first failure, so only the checks that actually ran are listed.
        """
        failed = None if self.ok else _failed_check(self.code, bool(self.current_user))
        passed_messages = {
            "dns": f"Resolved to {self.resolved_ip}." if self.resolved_ip else "The host name resolved.",
            "ssrf": "The address passed the host allow-list (no private, loopback or link-local targets).",
            "tls": f"Connected in {self.latency_ms} ms.",
            "login": (f"Logged in as {self.current_user} to {self.database} (PostgreSQL {self.server_version})."
                      if self.current_user else "Logged in."),
            "readonly": "No write, superuser or server-file privileges.",
        }
        out: list[dict[str, Any]] = []
        for key in CHECK_ORDER:
            if key == failed:
                message = self.message or "The check failed."
                if key == "readonly" and self.readonly.problems:
                    shown = ", ".join(self.readonly.problems[:5])
                    more = " …" if len(self.readonly.problems) > 5 else ""
                    message = f"{message} Problems: {shown}{more}"
                out.append({"key": key, "ok": False, "message": message})
                break
            out.append({"key": key, "ok": True, "message": passed_messages[key]})
        return out


CHECK_ORDER = ("dns", "ssrf", "tls", "login", "readonly")
_FAILED_CHECK_BY_CODE = {
    "connector_host_invalid": "dns",
    "connector_host_unresolvable": "dns",
    "connector_host_forbidden": "ssrf",
    "connector_connection_failed": "tls",   # TCP connect + TLS handshake are one step
    "connector_timeout": "tls",
    "connector_tls_failed": "tls",
    "connector_auth_failed": "login",
    "connector_role_not_readonly": "readonly",
}


def _failed_check(code: str | None, logged_in: bool) -> str:
    """The check a failure code belongs to (unknown codes: after login -> readonly, before -> tls)."""
    return _FAILED_CHECK_BY_CODE.get(code or "", "readonly" if logged_in else "tls")


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
