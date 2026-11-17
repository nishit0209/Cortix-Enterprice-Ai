"""Test doubles for the AI platform (used by apps.ai / apps.nl2sql tests and available to QA).

NEVER used in production code paths: production wiring lives in apps.ai.services.

* FakeTransport  — scripted Gemini transport that records every request (a network spy: `.calls`).
* FakeRedis      — in-memory subset of redis-py (get/set/mget/incrby/expire/delete/transaction) with a
                   controllable clock, enough for QuotaLimiter.
* MemoryRecorder — collects RequestRecords instead of writing AIRequestLog rows.
* build_engine() — a GeminiEngine wired with the fakes above.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .config import OrgAIConfig
from .engine import GeminiEngine
from .quota import ModelLimits, QuotaLimiter
from .recorder import RequestRecord
from .transport import GenerateRequest, RawResult, StreamChunk, TransportError, Usage


class FakeClock:
    def __init__(self, start: float = 1_790_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeTransport:
    """Scripted responses, consumed in order. Items: RawResult | TransportError | Exception |
    callable(GenerateRequest) -> RawResult | list[StreamChunk|TransportError] (for stream())."""

    def __init__(self, responses: list | None = None, *, default: RawResult | None = None):
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[GenerateRequest] = []
        self.stream_calls: list[GenerateRequest] = []

    @staticmethod
    def json(obj, *, input_tokens: int = 120, output_tokens: int = 40, cached_tokens: int = 0) -> RawResult:
        return RawResult(text=json.dumps(obj), usage=Usage(input_tokens, output_tokens, cached_tokens),
                         finish_reason="STOP")

    @staticmethod
    def text(text: str, *, input_tokens: int = 100, output_tokens: int = 20) -> RawResult:
        return RawResult(text=text, usage=Usage(input_tokens, output_tokens, 0), finish_reason="STOP")

    def push(self, *items) -> FakeTransport:
        self.responses.extend(items)
        return self

    def _next(self, request: GenerateRequest):
        if self.responses:
            item = self.responses.pop(0)
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeTransport: unexpected Gemini call (no scripted response left)")
        if callable(item) and not isinstance(item, (RawResult, Exception, list)):
            item = item(request)
        return item

    def generate(self, request: GenerateRequest) -> RawResult:
        self.calls.append(request)
        item = self._next(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, list):
            raise AssertionError("FakeTransport: stream script consumed by generate()")
        return item

    def stream(self, request: GenerateRequest) -> Iterator[StreamChunk]:
        self.stream_calls.append(request)
        item = self._next(request)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, RawResult):
            item = [StreamChunk(text=item.text or "", usage=item.usage, finish_reason=item.finish_reason)]
        for chunk in item:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    @property
    def total_calls(self) -> int:
        return len(self.calls) + len(self.stream_calls)


def rate_limited(kind: str = "rate_minute", retry_delay_s: float | None = None) -> TransportError:
    return TransportError(kind, "429 RESOURCE_EXHAUSTED", retry_delay_s=retry_delay_s, http_status=429)


def unavailable() -> TransportError:
    return TransportError("unavailable", "503 UNAVAILABLE", http_status=503)


class _Entry:
    __slots__ = ("value", "expire_at")

    def __init__(self, value: bytes, expire_at: float | None):
        self.value = value
        self.expire_at = expire_at


class FakeRedis:
    """Single-process stand-in for the redis-py client (bytes values, TTLs on a fake clock)."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._data: dict[str, _Entry] = {}
        self._clock = clock or FakeClock()
        self.fail = False  # set True to simulate an outage

    def _check(self):
        if self.fail:
            raise ConnectionError("FakeRedis: simulated outage")

    def _live(self, key: str) -> _Entry | None:
        e = self._data.get(key)
        if e is None:
            return None
        if e.expire_at is not None and e.expire_at <= self._clock():
            del self._data[key]
            return None
        return e

    @staticmethod
    def _b(value) -> bytes:
        if isinstance(value, bytes):
            return value
        return str(value).encode()

    def get(self, key):
        self._check()
        e = self._live(key)
        return e.value if e else None

    def mget(self, keys, *args):
        self._check()
        keys = list(keys) + list(args) if not isinstance(keys, str) else [keys, *args]
        return [self.get(k) for k in keys]

    def set(self, key, value, ex=None, px=None, exat=None, nx=False):
        self._check()
        if nx and self._live(key):
            return None
        now = self._clock()
        expire_at = None
        if ex is not None:
            expire_at = now + float(ex)
        elif px is not None:
            expire_at = now + float(px) / 1000
        elif exat is not None:
            expire_at = float(exat)
        self._data[key] = _Entry(self._b(value), expire_at)
        return True

    def incrby(self, key, amount=1):
        self._check()
        e = self._live(key)
        current = int(e.value) if e else 0
        new = current + int(amount)
        self._data[key] = _Entry(self._b(new), e.expire_at if e else None)
        return new

    def incr(self, key, amount=1):
        return self.incrby(key, amount)

    def expire(self, key, seconds):
        self._check()
        e = self._live(key)
        if not e:
            return False
        e.expire_at = self._clock() + float(seconds)
        return True

    def ttl(self, key):
        e = self._live(key)
        if not e:
            return -2
        if e.expire_at is None:
            return -1
        return int(e.expire_at - self._clock())

    def delete(self, *keys):
        self._check()
        n = 0
        for k in keys:
            if self._live(k):
                del self._data[k]
                n += 1
        return n

    def keys(self, pattern: str = "*"):
        import fnmatch

        return [k.encode() for k in list(self._data) if self._live(k) and fnmatch.fnmatch(k, pattern)]

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)

    def transaction(self, func, *watches, value_from_callable: bool = False, **_kw):
        self._check()
        pipe = _FakePipeline(self)
        pipe.watch(*watches)
        result = func(pipe)
        exec_value = pipe.execute()
        return result if value_from_callable else exec_value


class _FakePipeline:
    def __init__(self, r: FakeRedis):
        self._r = r
        self._queued: list[tuple[str, tuple, dict]] = []
        self._multi = False

    def watch(self, *keys):
        return True

    def unwatch(self):
        return True

    def multi(self):
        self._multi = True

    def reset(self):
        self._queued.clear()
        self._multi = False

    def __getattr__(self, name):
        target = getattr(self._r, name)

        def method(*args, **kwargs):
            if self._multi:
                self._queued.append((name, args, kwargs))
                return self
            return target(*args, **kwargs)

        return method

    def execute(self):
        out = [getattr(self._r, n)(*a, **k) for n, a, k in self._queued]
        self.reset()
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.reset()
        return False


@dataclass
class MemoryRecorder:
    records: list[RequestRecord] = field(default_factory=list)

    def record(self, rec: RequestRecord) -> UUID:
        self.records.append(rec)
        return uuid4()

    @property
    def statuses(self) -> list[str]:
        return [r.status for r in self.records]


def allow_all_policy(payload):
    """Policy check stub that allows everything (for tests that are not about the policy gate)."""
    from .policy import evaluate_policy

    return evaluate_policy(payload, state={"policy": "synthetic_only", "consent_valid": True},
                           classify=lambda ref: "synthetic", tier="free")


def build_engine(*, transport: FakeTransport | None = None, redis: FakeRedis | None = None,
                 clock: FakeClock | None = None, recorder: MemoryRecorder | None = None,
                 org_config: OrgAIConfig | None = None, model_limits: ModelLimits | None = None,
                 policy_check=None, sleep=None, rng=None, **kwargs) -> tuple[GeminiEngine, dict]:
    clock = clock or FakeClock()
    redis = redis or FakeRedis(clock)
    transport = transport or FakeTransport()
    recorder = recorder or MemoryRecorder()
    sleeps: list[float] = []
    limiter = QuotaLimiter(lambda: redis, clock=clock)
    engine = GeminiEngine(
        transport_factory=lambda: transport,
        limiter=limiter,
        recorder=recorder,
        org_config_loader=lambda org_id: org_config or OrgAIConfig(),
        model_limits_loader=lambda model: model_limits or ModelLimits(rpm=1000, tpm=10_000_000, rpd=10_000),
        policy_check=policy_check or allow_all_policy,
        sleep=sleep or (lambda s: (sleeps.append(s), clock.advance(s))),
        rng=rng or (lambda: 0.0),
        **kwargs,
    )
    return engine, {"transport": transport, "redis": redis, "clock": clock, "recorder": recorder,
                    "limiter": limiter, "sleeps": sleeps}
