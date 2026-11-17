import { describe, expect, it, vi } from "vitest";
import { SSEParser, openEventStream, parseEventData, type SSEMessage, type StreamState } from "./sse";
import { jobReducer, type JobProgressState } from "@/hooks/use-job-stream";

function streamResponse(chunks: string[], init: { status?: number } = {}) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: init.status ?? 200, headers: { "content-type": "text/event-stream" } });
}

describe("SSEParser", () => {
  it("parses id/event/data and keeps the last event id", () => {
    const p = new SSEParser();
    const msgs = p.feed('retry: 3000\n\nid: 1-0\nevent: status\ndata: {"status":"running","progress":10}\n\n');
    expect(p.retry).toBe(3000);
    expect(msgs).toEqual([{ id: "1-0", event: "status", data: '{"status":"running","progress":10}' }]);
    expect(p.lastEventId).toBe("1-0");
  });

  it("handles messages split across arbitrary chunk boundaries (including CRLF splits)", () => {
    const raw = 'id: 5-1\r\nevent: log\r\ndata: {"level":"info","message":"Parsing"}\r\n\r\n';
    const p = new SSEParser();
    const out: SSEMessage[] = [];
    for (const ch of raw) out.push(...p.feed(ch));
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({ id: "5-1", event: "log", data: '{"level":"info","message":"Parsing"}' });
  });

  it("joins multi-line data, ignores heartbeat comments and defaults the event name", () => {
    const p = new SSEParser();
    const msgs = p.feed(":hb\n\ndata: line1\ndata: line2\n\n:hb\n\n");
    expect(p.comments).toBe(2);
    expect(msgs).toEqual([{ id: null, event: "message", data: "line1\nline2" }]);
  });

  it("does not dispatch events without data and strips only one leading space", () => {
    const p = new SSEParser();
    expect(p.feed("event: status\n\n")).toEqual([]);
    expect(p.feed("data:  two-spaces\n\n")[0].data).toBe(" two-spaces");
  });

  it("parseEventData returns null on malformed JSON", () => {
    expect(parseEventData({ id: null, event: "x", data: "{oops" })).toBeNull();
    expect(parseEventData<{ a: number }>({ id: null, event: "x", data: '{"a":1}' })).toEqual({ a: 1 });
  });
});

describe("openEventStream", () => {
  it("streams events until the terminal event, reporting ids", async () => {
    const fetchImpl = vi.fn(async () =>
      streamResponse([
        "retry: 10\n\n",
        'id: 1-0\nevent: status\ndata: {"status":"running","progress":20}\n\n',
        'id: 2-0\nevent: result\ndata: {"result":{"rows":10}}\n\nid: 3-0\nevent: done\ndata: {"status":"succeeded"}\n\n',
      ]),
    );
    const seen: SSEMessage[] = [];
    const ids: string[] = [];
    const states: StreamState[] = [];
    await openEventStream({
      url: "/orgs/acme/jobs/j1/events",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: (m) => seen.push(m),
      onEventId: (id) => ids.push(id),
      onStateChange: (s) => states.push(s),
      sleep: async () => {},
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(seen.map((m) => m.event)).toEqual(["status", "result", "done"]);
    expect(ids).toEqual(["1-0", "2-0", "3-0"]);
    expect(states[0]).toBe("connecting");
    expect(states).toContain("open");
    expect(states.at(-1)).toBe("closed");
  });

  it("reconnects with Last-Event-ID when the server closes before `done`", async () => {
    const headersSeen: Array<Record<string, string>> = [];
    let call = 0;
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      headersSeen.push(init.headers as Record<string, string>);
      call++;
      if (call === 1) return streamResponse(['id: 7-0\nevent: status\ndata: {"progress":50}\n\n']);
      return streamResponse(['id: 8-0\nevent: done\ndata: {"status":"succeeded"}\n\n']);
    });
    const events: string[] = [];
    await openEventStream({
      url: "/orgs/acme/jobs/j1/events",
      lastEventId: "6-0",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: (m) => events.push(m.event),
      sleep: async () => {},
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(headersSeen[0]["Last-Event-ID"]).toBe("6-0");
    expect(headersSeen[1]["Last-Event-ID"]).toBe("7-0");
    expect(headersSeen[0].Accept).toBe("text/event-stream");
    expect(events).toEqual(["status", "done"]);
  });

  it("stops (no retry) on client errors such as 404", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ code: "not_found", status: 404 }), { status: 404, headers: { "content-type": "application/json" } }));
    const states: Array<[StreamState, string | undefined]> = [];
    await openEventStream({
      url: "/orgs/acme/jobs/missing/events",
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: () => {},
      onStateChange: (s, e) => states.push([s, e?.code]),
      sleep: async () => {},
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(states.at(-1)).toEqual(["error", "not_found"]);
  });

  it("gives up after maxRetries on repeated network failures", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("network down");
    });
    const states: StreamState[] = [];
    await openEventStream({
      url: "/x",
      maxRetries: 2,
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onMessage: () => {},
      onStateChange: (s) => states.push(s),
      sleep: async () => {},
    });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(states.at(-1)).toBe("error");
  });
});

describe("jobReducer", () => {
  const initial: JobProgressState = {
    status: "connecting",
    progress: null,
    message: null,
    logs: [],
    result: null,
    error: null,
    done: false,
  };

  it("folds status, log, result and done events", () => {
    let s = jobReducer(initial, { kind: "event", event: { type: "status", id: "1", data: { status: "running", progress: 30, message: "Profiling" } } });
    expect(s).toMatchObject({ status: "running", progress: 30, message: "Profiling" });
    s = jobReducer(s, { kind: "event", event: { type: "log", id: "2", data: { level: "warning", message: "3 bad rows", step_key: "parse" } } });
    expect(s.logs).toHaveLength(1);
    expect(s.logs[0]).toMatchObject({ level: "warning", step_key: "parse" });
    s = jobReducer(s, { kind: "event", event: { type: "result", id: "3", data: { result: { dataset_id: "d1" } } } });
    expect(s.result).toEqual({ dataset_id: "d1" });
    s = jobReducer(s, { kind: "event", event: { type: "done", id: "4", data: { status: "succeeded" } } });
    expect(s).toMatchObject({ status: "succeeded", progress: 100, done: true });
  });

  it("records errors and applies REST snapshots", () => {
    let s = jobReducer(initial, { kind: "event", event: { type: "error", id: "1", data: { code: "upload_rejected", message: "Bad file" } } });
    expect(s.status).toBe("failed");
    expect(s.error).toEqual({ code: "upload_rejected", message: "Bad file" });
    s = jobReducer(initial, {
      kind: "snapshot",
      job: {
        id: "j",
        type: "ingest",
        status: "succeeded",
        progress: 100,
        message: "Done",
        result: null,
        error: null,
        created_at: "2026-11-17T00:00:00Z",
        finished_at: "2026-11-17T00:01:00Z",
      },
    });
    expect(s.done).toBe(true);
    expect(s.status).toBe("succeeded");
  });
});
