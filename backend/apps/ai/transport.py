"""SDK-neutral transport interface used by the GeminiService engine.

The real implementation (`apps.ai.gemini.GenaiTransport`) is the ONLY code that imports `google.genai`.
Tests inject `apps.ai.testing.FakeTransport`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

TransportErrorKind = Literal["rate_minute", "rate_day", "unavailable", "timeout", "bad_request", "auth", "unknown"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class RawResult:
    text: str | None
    usage: Usage
    finish_reason: str | None = None
    block_reason: str | None = None  # prompt-level safety block

    @property
    def safety_blocked(self) -> bool:
        return bool(self.block_reason) or (self.finish_reason or "").upper() in {
            "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION",
        }


@dataclass(frozen=True)
class StreamChunk:
    text: str
    usage: Usage | None = None
    finish_reason: str | None = None
    block_reason: str | None = None


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    system: str
    prompt: str
    temperature: float
    max_output_tokens: int
    timeout_s: float
    response_schema: dict | None = None


class TransportError(Exception):
    def __init__(self, kind: TransportErrorKind, message: str = "", *, retry_delay_s: float | None = None,
                 http_status: int | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.retry_delay_s = retry_delay_s
        self.http_status = http_status

    @property
    def retryable(self) -> bool:
        return self.kind in ("rate_minute", "unavailable", "timeout")


class Transport(Protocol):
    def generate(self, request: GenerateRequest) -> RawResult: ...

    def stream(self, request: GenerateRequest) -> Iterator[StreamChunk]: ...
