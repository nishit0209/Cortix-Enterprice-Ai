"""Network doubles for connector tests: fake DNS answers and a psycopg connection spy (nothing leaves the process)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class Resolver:
    """Fake DNS: name -> list of answers, consumed in order (the last one repeats). Other names resolve normally."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    def install(self, monkeypatch) -> Resolver:
        real = socket.getaddrinfo

        def fake(host, port, family=0, type=0, proto=0, flags=0):
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name in self.answers:
                self.lookups.append(name)
                seq = self.answers[name]
                ip = seq.pop(0) if len(seq) > 1 else seq[0]
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                sockaddr = (ip, int(port or 0), 0, 0) if fam == socket.AF_INET6 else (ip, int(port or 0))
                return [(fam, socket.SOCK_STREAM, 6, "", sockaddr)]
            return real(host, port, family, type, proto, flags)

        monkeypatch.setattr(socket, "getaddrinfo", fake)
        # The as-built guard binds `resolver=socket.getaddrinfo` as a keyword default at import time, so patching
        # the socket module alone never reaches it: swap the bound default as well (restored by monkeypatch).
        try:
            from apps.connectors import ssrf
        except ImportError:
            ssrf = None
        kwdefaults = getattr(getattr(ssrf, "resolve_and_check", None), "__kwdefaults__", None)
        if kwdefaults and "resolver" in kwdefaults:
            monkeypatch.setitem(kwdefaults, "resolver", fake)
        return self


@dataclass
class ConnectSpy:
    """Captures every psycopg connection attempt to the probe database (`PROBE_DB`) and refuses it.

    Connections to any other database (Django's own, the warehouse) pass through untouched. `error_message`
    lets a test make the refusal look like a driver error (e.g. one that echoes connection parameters)."""

    PROBE_DB: ClassVar[str] = "ssrf_probe_db"
    attempts: list[dict] = field(default_factory=list)
    error_message: str = "connection refused by the QA connect spy"

    def install(self, monkeypatch) -> ConnectSpy:
        import psycopg

        spy = self
        original = psycopg.Connection.connect.__func__

        def connect(cls, conninfo="", **kwargs):
            params = dict(psycopg.conninfo.conninfo_to_dict(conninfo, **kwargs)) if conninfo or kwargs else {}
            if params.get("dbname") != spy.PROBE_DB:
                return original(cls, conninfo, **kwargs)
            spy.attempts.append(params)
            raise psycopg.OperationalError(spy.error_message.format(**{k: str(v) for k, v in params.items()}))

        monkeypatch.setattr(psycopg.Connection, "connect", classmethod(connect))
        # `psycopg.connect` is bound to the original classmethod at import time: patch it as well
        monkeypatch.setattr(
            psycopg, "connect", lambda conninfo="", **kw: connect(psycopg.Connection, conninfo, **kw)
        )
        return self

    def targets(self) -> list[str]:
        return [str(a.get("hostaddr") or a.get("host") or "") for a in self.attempts]
