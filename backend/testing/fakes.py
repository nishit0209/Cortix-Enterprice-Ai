"""Test doubles shared by every agent. Tests must NEVER call Gemini live.

FakeGeminiService  implements nexora.contracts.GeminiService; scripted responses, full call capture.
NetworkSpy         blocks + records every outbound socket connection except to local infra.
FakeRedis          in-memory subset of redis-py used by apps.core.jobs (xadd/xread/xrange/expire/delete).
FakeClock          deterministic `timezone.now()` replacement.
"""

from __future__ import annotations

import copy
import datetime as dt
import ipaddress
import itertools
import json
import socket
import threading
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------------------------- Gemini
@dataclass
class GeminiCall:
    method: str  # "generate_structured" | "generate_text" | "stream"
    feature: str
    prompt_key: str
    system: str
    prompt: str
    payload: Any
    tier: str
    schema: dict | None = None
    extra: dict = field(default_factory=dict)  # user_id, priority, temperature, ... (engine extensions)

    @property
    def text(self) -> str:
        """Everything that would have left the process (system + prompt)."""
        return f"{self.system}\n{self.prompt}"


class FakeGeminiError(RuntimeError):
    pass


@dataclass
class FakeGeminiService:
    """Scripted stand-in for `apps.ai.services.gemini()`.

        fake = FakeGeminiService()
        fake.respond("nl2sql.generate", {"sql": "SELECT 1"})       # by prompt_key or feature
        fake.respond("narrate", stream=["Revenue ", "grew 4%"])
        fake.fail("nl2sql.generate", AIUnavailable())               # raise on the next matching call
        ...
        assert fake.calls[0].payload.items == {...}
        fake.assert_not_in_prompts("CANARY-VALUE-123")

    Responses are matched by prompt_key first, then feature, then the "*" default. A list of responses is
    consumed in order (the last one repeats). Unscripted calls raise FakeGeminiError so tests notice.

    `policy_check` (e.g. apps.ai.services.check_policy) runs the REAL AIDataPolicy gate before anything is
    recorded as sent: a blocked call raises the gate's exception, lands in `.blocked` and never in `.calls`
    (`.calls` == what would have left the process).
    """

    calls: list[GeminiCall] = field(default_factory=list)
    blocked: list[GeminiCall] = field(default_factory=list)
    policy_check: Callable[[Any], Any] | None = None
    _structured: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    _streams: dict[str, list[list[str]]] = field(default_factory=lambda: defaultdict(list))
    _errors: dict[str, list[BaseException]] = field(default_factory=lambda: defaultdict(list))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # scripting --------------------------------------------------------------------------------
    def respond(
        self, key: str, structured: Any = None, *, stream: list[str] | None = None
    ) -> FakeGeminiService:
        if structured is not None:
            self._structured[key].append(structured)
        if stream is not None:
            self._streams[key].append(list(stream))
        return self

    def fail(self, key: str, exc: BaseException) -> FakeGeminiService:
        self._errors[key].append(exc)
        return self

    # contract ---------------------------------------------------------------------------------
    def generate_structured(
        self,
        *,
        feature: str,
        prompt_key: str,
        system: str,
        prompt: str,
        schema: dict,
        payload,
        tier: str = "fast",
        **extra: Any,
    ) -> dict:
        self._record("generate_structured", feature, prompt_key, system, prompt, payload, tier, schema, extra)
        self._maybe_raise(prompt_key, feature)
        value = self._take(self._structured, prompt_key, feature)
        if value is None:
            raise FakeGeminiError(f"no scripted structured response for {prompt_key!r}/{feature!r}")
        return copy.deepcopy(value(prompt) if callable(value) else value)

    def generate_text(
        self, *, feature: str, prompt_key: str, system: str, prompt: str, payload, tier: str = "fast", **extra: Any
    ) -> str:
        self._record("generate_text", feature, prompt_key, system, prompt, payload, tier, None, extra)
        self._maybe_raise(prompt_key, feature)
        chunks = self._take(self._streams, prompt_key, feature)
        if chunks is not None:
            return "".join(chunks)
        value = self._take(self._structured, prompt_key, feature)
        if value is None:
            raise FakeGeminiError(f"no scripted text for {prompt_key!r}/{feature!r}")
        return value if isinstance(value, str) else json.dumps(value)

    def stream(
        self, *, feature: str, prompt_key: str, system: str, prompt: str, payload, tier: str = "fast", **extra: Any
    ) -> Iterator[str]:
        # the real engine checks policy/quota eagerly, before the first chunk is requested
        self._record("stream", feature, prompt_key, system, prompt, payload, tier, None, extra)
        self._maybe_raise(prompt_key, feature)
        chunks = self._take(self._streams, prompt_key, feature)
        if chunks is None:
            raise FakeGeminiError(f"no scripted stream for {prompt_key!r}/{feature!r}")
        return iter(list(chunks))

    # assertions -------------------------------------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.calls)

    def prompts(self) -> list[str]:
        return [c.text for c in self.calls]

    def assert_not_in_prompts(self, *needles: str) -> None:
        for call in self.calls:
            for needle in needles:
                assert needle not in call.text, f"{needle!r} leaked into Gemini prompt {call.prompt_key!r}"

    def assert_no_calls(self) -> None:
        assert not self.calls, f"expected no Gemini calls, got {[c.prompt_key for c in self.calls]}"

    # internals --------------------------------------------------------------------------------
    def _record(self, method, feature, prompt_key, system, prompt, payload, tier, schema, extra=None) -> None:
        call = GeminiCall(method, feature, prompt_key, system, prompt, payload, tier, schema, dict(extra or {}))
        if self.policy_check is not None:
            try:
                self.policy_check(payload)
            except BaseException:
                with self._lock:
                    self.blocked.append(call)
                raise
        with self._lock:
            self.calls.append(call)

    def _maybe_raise(self, *keys: str) -> None:
        for key in (*keys, "*"):
            if self._errors.get(key):
                raise self._errors[key].pop(0)

    @staticmethod
    def _take(store: dict[str, list[Any]], *keys: str):
        for key in (*keys, "*"):
            items = store.get(key)
            if items:
                return items.pop(0) if len(items) > 1 else items[0]
        return None


# --------------------------------------------------------------------------------------------- network
@dataclass
class NetworkSpy:
    """Records (and by default blocks) outbound connections to anything that is not local infra.

    with NetworkSpy() as spy:
        run_feature()
    assert spy.external == []
    """

    block: bool = True
    allow_hosts: tuple[str, ...] = ("localhost",)
    attempts: list[tuple[str, int]] = field(default_factory=list)
    _orig_connect: Callable | None = None
    _orig_create: Callable | None = None
    _orig_getaddrinfo: Callable | None = None

    @staticmethod
    def _is_local(host: str) -> bool:
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host in ("localhost",) or host.endswith(".localhost")

    @property
    def external(self) -> list[tuple[str, int]]:
        return [(h, p) for h, p in self.attempts if not (self._is_local(h) or h in self.allow_hosts)]

    def _check(self, host: str, port: int) -> None:
        self.attempts.append((host, port))
        if self.block and not (self._is_local(host) or host in self.allow_hosts):
            raise ConnectionRefusedError(f"NetworkSpy blocked outbound connection to {host}:{port}")

    def __enter__(self) -> NetworkSpy:
        spy = self
        self._orig_connect = socket.socket.connect
        self._orig_create = socket.create_connection
        self._orig_getaddrinfo = socket.getaddrinfo

        def connect(sock, address):
            if isinstance(address, tuple) and len(address) >= 2:
                spy._check(str(address[0]), int(address[1]))
            return spy._orig_connect(sock, address)

        def create_connection(address, *args, **kwargs):
            spy._check(str(address[0]), int(address[1]))
            return spy._orig_create(address, *args, **kwargs)

        def getaddrinfo(host, port, *args, **kwargs):
            if host is not None:
                h = host.decode() if isinstance(host, bytes) else str(host)
                if spy.block and not (spy._is_local(h) or h in spy.allow_hosts):
                    spy.attempts.append((h, int(port or 0)))
                    raise socket.gaierror(f"NetworkSpy blocked DNS lookup for {h}")
            return spy._orig_getaddrinfo(host, port, *args, **kwargs)

        socket.socket.connect = connect  # type: ignore[method-assign]
        socket.create_connection = create_connection  # type: ignore[assignment]
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        socket.socket.connect = self._orig_connect  # type: ignore[method-assign]
        socket.create_connection = self._orig_create  # type: ignore[assignment]
        socket.getaddrinfo = self._orig_getaddrinfo  # type: ignore[assignment]


# --------------------------------------------------------------------------------------------- redis
class FakeRedis:
    """Minimal in-memory Redis Streams for apps.core.jobs / SSE tests (single process)."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[bytes, bytes]]]] = defaultdict(list)
        self.ttl: dict[str, int] = {}
        self._ids = itertools.count(1)

    @staticmethod
    def _b(v) -> bytes:
        return v if isinstance(v, bytes) else str(v).encode()

    def xadd(self, key, fields: dict, maxlen: int | None = None, approximate: bool = True, **_kw) -> bytes:
        key = key.decode() if isinstance(key, bytes) else key
        eid = f"{next(self._ids)}-0"
        self.streams[key].append((eid, {self._b(k): self._b(v) for k, v in fields.items()}))
        if maxlen is not None and len(self.streams[key]) > maxlen:
            self.streams[key] = self.streams[key][-maxlen:]
        return eid.encode()

    def expire(self, key, seconds: int) -> bool:
        self.ttl[key.decode() if isinstance(key, bytes) else key] = int(seconds)
        return True

    def xrange(self, key, min="-", max="+", count=None):
        items = [(e.encode(), f) for e, f in self.streams.get(key, [])]
        return items[:count] if count else items

    def xread(self, streams: dict, count: int | None = None, block: int | None = None):
        out = []
        for key, last in streams.items():
            last_n = int(str(last.decode() if isinstance(last, bytes) else last).split("-")[0])
            items = [(e.encode(), f) for e, f in self.streams.get(key, []) if int(e.split("-")[0]) > last_n]
            if count:
                items = items[:count]
            if items:
                out.append((key.encode(), items))
        return out

    def delete(self, *keys) -> int:
        n = 0
        for k in keys:
            n += 1 if self.streams.pop(k, None) is not None else 0
        return n

    def keys(self, pattern: str = "*") -> list[bytes]:
        import fnmatch

        return [k.encode() for k in self.streams if fnmatch.fnmatch(k, pattern)]

    def events(self, key: str) -> list[tuple[str, dict]]:
        """Decoded (event, data) pairs of a stream — convenience for assertions."""
        return [
            (f[b"event"].decode(), json.loads(f[b"data"].decode())) for _eid, f in self.streams.get(key, [])
        ]


# --------------------------------------------------------------------------------------------- clock
class FakeClock:
    """Deterministic clock. `install(monkeypatch)` patches django.utils.timezone.now."""

    def __init__(self, start: dt.datetime | None = None) -> None:
        self.current = start or dt.datetime(2026, 11, 17, 9, 0, tzinfo=dt.UTC)

    def now(self) -> dt.datetime:
        return self.current

    def advance(self, **delta: float) -> dt.datetime:
        self.current += dt.timedelta(**delta)
        return self.current

    def install(self, monkeypatch) -> FakeClock:
        from django.utils import timezone

        monkeypatch.setattr(timezone, "now", self.now)
        return self
