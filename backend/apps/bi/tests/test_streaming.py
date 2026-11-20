"""SSE framing for the chat stream (no database)."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from django.test import RequestFactory

from apps.bi import streaming


def parse(chunks: list[str]) -> list[tuple[str, dict]]:
    out = []
    for chunk in chunks:
        if chunk.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in chunk.strip().split("\n"))
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_format_event_is_one_sse_frame():
    frame = streaming.format_event(3, "insight", {"kind": "top_n", "text": "West leads – ₹5,400"})
    assert frame.startswith("id: 3\nevent: insight\ndata: {") and frame.endswith("}\n\n")
    assert frame.count("\n\n") == 1
    assert json.loads(frame.split("data: ", 1)[1]) == {"kind": "top_n", "text": "West leads – ₹5,400"}


def test_sync_stream_frames_events_in_order_and_closes_the_pipeline():
    closed = []

    def pipeline():
        try:
            yield ("status", {"stage": "authorizing"})
            yield ("done", {"message_id": "m1"})
        finally:
            closed.append(True)

    chunks = list(streaming.sync_stream(pipeline))
    assert chunks[0] == ": stream-open\n\n"
    assert parse(chunks) == [("status", {"stage": "authorizing"}), ("done", {"message_id": "m1"})]
    assert closed == [True]


def test_sync_stream_client_disconnect_closes_the_generator():
    state = []

    def pipeline():
        try:
            yield ("status", {"stage": "authorizing"})
            yield ("status", {"stage": "resolving"})
        except GeneratorExit:
            state.append("cancelled")
            raise

    body = streaming.sync_stream(pipeline)
    next(body), next(body)
    body.close()
    assert state == ["cancelled"]


def test_async_stream_sends_keepalives_and_runs_the_pipeline_in_one_thread():
    threads = set()
    exits = []

    def pipeline():
        threads.add(threading.get_ident())
        yield ("status", {"stage": "generating"})
        time.sleep(0.12)  # e.g. a Gemini call
        threads.add(threading.get_ident())
        yield ("done", {"message_id": "m1"})

    async def consume():
        return [c async for c in streaming.async_stream(pipeline, keepalive_s=0.03,
                                                         on_thread_exit=lambda: exits.append(1))]

    chunks = asyncio.run(consume())
    assert chunks[0] == ": stream-open\n\n"
    assert ": keep-alive\n\n" in chunks
    assert parse(chunks) == [("status", {"stage": "generating"}), ("done", {"message_id": "m1"})]
    assert len(threads) == 1 and threading.get_ident() not in threads
    assert exits == [1]


def test_async_stream_turns_a_crash_into_an_error_event():
    def pipeline():
        yield ("status", {"stage": "authorizing"})
        raise RuntimeError("boom")

    async def consume():
        return [c async for c in streaming.async_stream(pipeline, keepalive_s=1)]

    events = parse(asyncio.run(consume()))
    assert events[-1][0] == "error" and events[-1][1]["code"] == "internal_error"
    assert "boom" not in json.dumps(events)


def test_async_stream_stops_the_pipeline_when_the_client_disconnects():
    produced = []
    finished = threading.Event()

    def pipeline():
        try:
            for i in range(1000):
                produced.append(i)
                yield ("status", {"stage": "executing", "i": i})
                time.sleep(0.01)
        finally:
            finished.set()

    async def consume_two():
        agen = streaming.async_stream(pipeline, keepalive_s=1)
        got = [await agen.__anext__() for _ in range(3)]
        await agen.aclose()
        await asyncio.sleep(0.1)
        return got

    asyncio.run(consume_two())
    assert finished.wait(2)
    assert len(produced) < 1000


def test_response_headers_for_wsgi_requests():
    request = RequestFactory().post("/x", HTTP_ACCEPT="text/event-stream")
    resp = streaming.event_stream_response(request, lambda: iter([("done", {"message_id": "m"})]))
    assert resp["Content-Type"].startswith("text/event-stream")
    assert resp["X-Accel-Buffering"] == "no"
    assert "no-cache" in resp["Cache-Control"]
    assert resp.streaming and not resp.is_async
    assert parse([c.decode() for c in resp.streaming_content]) == [("done", {"message_id": "m"})]
    assert streaming.is_asgi(request) is False
