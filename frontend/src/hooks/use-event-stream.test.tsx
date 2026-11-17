import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useEventStream, type TypedEvent } from "./use-event-stream";
import type { JobEventMap } from "@/lib/types";

function streamResponse(chunks: string[]) {
  const enc = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(c) {
        chunks.forEach((x) => c.enqueue(enc.encode(x)));
        c.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

describe("useEventStream", () => {
  it("delivers typed, JSON-decoded events, persists the resume id and clears it on done", async () => {
    window.sessionStorage.setItem("nexora:sse:job:j1", "41-0");
    const fetchImpl = vi.fn(async (_u: string, init: RequestInit) => {
      expect((init.headers as Record<string, string>)["Last-Event-ID"]).toBe("41-0");
      return streamResponse([
        'id: 42-0\nevent: status\ndata: {"status":"running","progress":60,"message":"Profiling"}\n\n',
        "id: 43-0\nevent: log\ndata: {not json}\n\n",
        'id: 44-0\nevent: done\ndata: {"status":"succeeded"}\n\n',
      ]);
    });
    const events: Array<TypedEvent<JobEventMap>> = [];
    const { result } = renderHook(() =>
      useEventStream<JobEventMap>("/orgs/acme/jobs/j1/events", {
        onEvent: (e) => events.push(e),
        resumeKey: "job:j1",
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    );
    await waitFor(() => expect(result.current.state).toBe("closed"));
    // The malformed log event is skipped; status and done are typed.
    expect(events.map((e) => e.type)).toEqual(["status", "done"]);
    const status = events[0];
    if (status.type === "status") expect(status.data.progress).toBe(60);
    expect(result.current.lastEventId).toBe("44-0");
    expect(window.sessionStorage.getItem("nexora:sse:job:j1")).toBeNull();
  });

  it("does nothing without a url", () => {
    const fetchImpl = vi.fn();
    const { result } = renderHook(() => useEventStream(null, { fetchImpl: fetchImpl as unknown as typeof fetch }));
    expect(result.current.state).toBe("idle");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
