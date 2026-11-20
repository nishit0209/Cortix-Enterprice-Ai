"""Server-Sent Events helpers for API tests (chat streams, job events)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: Any
    id: str | None = None


def parse_sse(text: str) -> list[SseEvent]:
    """Parse an SSE body (blank-line separated blocks; `data:` lines joined by newlines; JSON decoded)."""
    events: list[SseEvent] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data_lines, eid = "message", [], None
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field_name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field_name == "event":
                name = value
            elif field_name == "data":
                data_lines.append(value)
            elif field_name == "id":
                eid = value
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        events.append(SseEvent(name, data, eid))
    return events


def read_stream(response, *, max_bytes: int = 20 * 1024 * 1024) -> str:
    """Text body of a (possibly streaming) Django test response; async iterators included."""
    return read_bytes(response, max_bytes=max_bytes).decode("utf-8", errors="replace")


def read_bytes(response, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
    """Raw body of a (possibly streaming) Django test response; async iterators included."""
    if not getattr(response, "streaming", False):
        return response.content
    content = response.streaming_content
    chunks: list[bytes] = []

    def add(chunk) -> bool:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
        return sum(map(len, chunks)) <= max_bytes

    if hasattr(content, "__aiter__"):
        # async_to_sync (not asyncio.run): thread-sensitive sync_to_async DB calls inside the stream then run on
        # THIS thread, so they see the test's transaction like the request itself did.
        from asgiref.sync import async_to_sync

        async def collect():
            async for chunk in content:
                if not add(chunk):
                    break

        async_to_sync(collect)()
    else:
        for chunk in content:
            if not add(chunk):
                break
    return b"".join(chunks)


def events_by_name(events: list[SseEvent]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for ev in events:
        out.setdefault(ev.event, []).append(ev.data)
    return out
