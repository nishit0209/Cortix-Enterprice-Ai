"""Server-Sent Events for the chat pipeline.

The view returns the response BEFORE the pipeline runs, so no request transaction is held while streaming:
`apps.bi.services.ask` opens its own short tenant transactions per step.

* ASGI (uvicorn, production): the synchronous pipeline runs in ONE worker thread (its DB connection stays on
  that thread and is closed at the end) and hands events to the event loop through a queue. While no event is
  ready a `: keep-alive` comment is sent every KEEPALIVE_S seconds. A client disconnect stops the pipeline at
  its next event (the assistant message is finalised as `cancelled`).
* WSGI / Django test client: the generator is iterated directly in the request thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator

from django.core.handlers.asgi import ASGIRequest
from django.core.serializers.json import DjangoJSONEncoder
from django.http import StreamingHttpResponse

log = logging.getLogger("nexora.bi")

KEEPALIVE_S = 10.0
Event = tuple[str, dict]
_END = object()


def format_event(seq: int, event: str, data: dict) -> str:
    payload = json.dumps(data, cls=DjangoJSONEncoder, separators=(",", ":"), ensure_ascii=False)
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n"


def sync_stream(make_events: Callable[[], Iterable[Event]]) -> Iterator[str]:
    yield ": stream-open\n\n"
    events = make_events()
    try:
        for seq, (event, data) in enumerate(events, 1):
            yield format_event(seq, event, data)
    finally:
        close = getattr(events, "close", None)
        if close:
            close()  # client went away: GeneratorExit inside the pipeline finalises the message


async def async_stream(make_events: Callable[[], Iterable[Event]], *, keepalive_s: float = KEEPALIVE_S,
                       on_thread_exit: Callable[[], None] | None = None) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def put(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # loop already closed (client gone)
            pass

    def produce() -> None:
        events = None
        try:
            events = iter(make_events())
            for item in events:
                put(item)
                if stop.is_set():
                    break
        except Exception:  # noqa: BLE001 - surfaced as a terminal error event
            log.exception("bi: chat stream producer failed")
            put(("error", {"code": "internal_error", "detail": "Something went wrong while answering."}))
        finally:
            close = getattr(events, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.exception("bi: closing the chat pipeline failed")
            if on_thread_exit is not None:
                on_thread_exit()
            put(_END)

    ctx = contextvars.copy_context()
    loop.run_in_executor(None, ctx.run, produce)
    seq = 0
    try:
        yield ": stream-open\n\n"
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is _END:
                break
            seq += 1
            yield format_event(seq, *item)
    finally:
        stop.set()


def _close_thread_connections() -> None:
    from django.db import connections

    connections.close_all()


def is_asgi(request) -> bool:
    raw = getattr(request, "_request", request)
    return isinstance(raw, ASGIRequest)


def event_stream_response(request, make_events: Callable[[], Iterable[Event]]) -> StreamingHttpResponse:
    if is_asgi(request):
        body = async_stream(make_events, on_thread_exit=_close_thread_connections)
    else:
        body = sync_stream(make_events)
    resp = StreamingHttpResponse(body, content_type="text/event-stream; charset=utf-8")
    resp["Cache-Control"] = "no-cache, no-transform"
    resp["X-Accel-Buffering"] = "no"  # nginx/ingress: do not buffer the stream
    return resp
