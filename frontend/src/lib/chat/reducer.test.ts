import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import { postEventStream } from "@/lib/post-stream";
import {
  chatReducer,
  initialChatState,
  nextStepAfterStream,
  toChatEvent,
  type ChatAction,
  type ChatEvent,
  type ChatStreamState,
} from "./reducer";

const ev = (event: ChatEvent): ChatAction => ({ kind: "event", event, now: 1000 });
const fold = (actions: ChatAction[], s: ChatStreamState = initialChatState(0)) => actions.reduce(chatReducer, s);

const SQL_EVENT: ChatEvent = {
  type: "sql",
  data: { raw_sql: "select region, sum(amount) from sales group by 1", executed_sql: "SELECT region, SUM(amount) FROM t GROUP BY 1 LIMIT 1001", source: "builder", query_audit_id: "qa1" },
};
const RESULT_EVENT: ChatEvent = {
  type: "result",
  data: { columns: [{ name: "region", type: "text" }, { name: "amount", type: "numeric" }], rows: [["West", 10]], row_count: 1, truncated: false, chart: { type: "bar", x: "region", y: ["amount"] } },
};

describe("chatReducer: event ordering", () => {
  it("folds a full happy-path stream into a completed answer", () => {
    const s = fold([
      { kind: "open" },
      ev({ type: "status", data: { stage: "authorizing" } }),
      ev({ type: "status", data: { stage: "resolving", source: "builder" } }),
      ev({ type: "status", data: { stage: "validating" } }),
      ev(SQL_EVENT),
      ev({ type: "status", data: { stage: "executing" } }),
      ev(RESULT_EVENT),
      ev({ type: "insight", data: { kind: "top_n", text: "West leads." } }),
      ev({ type: "status", data: { stage: "explaining" } }),
      ev({ type: "token", data: { text: "West " } }),
      ev({ type: "token", data: { text: "leads." } }),
      ev({ type: "done", data: { message_id: "m1" } }),
    ]);
    expect(s.phase).toBe("done");
    expect(s.stage).toBe("explaining");
    expect(s.seenStages).toEqual(["authorizing", "resolving", "validating", "executing", "explaining"]);
    expect(s.sql).toMatchObject({ source: "builder", executed: expect.stringContaining("LIMIT 1001"), queryAuditId: "qa1" });
    expect(s.result?.row_count).toBe(1);
    expect(s.chart).toEqual({ type: "bar", x: "region", y: ["amount"] });
    expect(s.narration).toBe("West leads.");
    expect(s.insights).toHaveLength(1);
    expect(s.messageId).toBe("m1");
    expect(s.received).toBe(11);
    expect(s.finishedAt).toBe(1000);
  });

  it("never moves the stage backwards on late or duplicate status events", () => {
    const s = fold([ev({ type: "status", data: { stage: "executing" } }), ev({ type: "status", data: { stage: "resolving" } })]);
    expect(s.stage).toBe("executing");
    expect(s.seenStages).toEqual(["executing", "resolving"]);
  });

  it("infers stages from sql/result/token when status events are skipped (cache hits)", () => {
    expect(fold([ev(SQL_EVENT)]).stage).toBe("validating");
    expect(fold([ev(RESULT_EVENT)]).stage).toBe("executing");
    expect(fold([ev(RESULT_EVENT), ev(SQL_EVENT)]).stage).toBe("executing");
    expect(fold([ev({ type: "token", data: { text: "x" } })]).stage).toBe("explaining");
  });

  it("accepts `chart_spec` as the chart field and de-duplicates insights", () => {
    const s = fold([
      ev({ type: "result", data: { ...RESULT_EVENT.data, chart: undefined, chart_spec: { type: "line", x: "d", y: "v" } } as never }),
      ev({ type: "insight", data: { kind: "trend", text: "Up 5%" } }),
      ev({ type: "insight", data: { kind: "trend", text: "Up 5%" } }),
    ]);
    expect(s.chart?.type).toBe("line");
    expect(s.insights).toHaveLength(1);
  });

  it("ignores every event after a terminal done/error", () => {
    const done = fold([ev({ type: "done", data: { message_id: "m1" } }), ev({ type: "token", data: { text: "late" } })]);
    expect(done.narration).toBe("");
    const err = fold([ev({ type: "error", data: { code: "ai_policy_blocked" } }), ev({ type: "done", data: { message_id: "m2" } })]);
    expect(err.phase).toBe("error");
    expect(err.messageId).toBeNull();
  });
});

describe("chatReducer: errors", () => {
  it("keeps the SQL that already arrived on a guard error", () => {
    const s = fold([ev(SQL_EVENT), ev({ type: "error", data: { code: "column_not_allowed", detail: "salary" } })]);
    expect(s.phase).toBe("error");
    expect(s.error).toMatchObject({ code: "column_not_allowed", detail: "salary" });
    expect(s.sql?.executed).toContain("SUM(amount)");
  });

  it("takes executed_sql from the error payload when no sql event came", () => {
    const s = fold([ev({ type: "error", data: { code: "sql_rejected", detail: "multiple_statements", reject_code: "multiple_statements", executed_sql: "select 1; drop" } })]);
    expect(s.sql?.executed).toBe("select 1; drop");
    expect(s.error?.rejectCode).toBe("multiple_statements");
  });

  it("carries quota resets_at from an HTTP rejection (problem+json body)", () => {
    const error = new ApiError({ status: 429, code: "ai_quota_exhausted", detail: "budget", body: { status: 429, code: "ai_quota_exhausted", resets_at: "2026-11-19T08:00:00Z" } });
    const s = chatReducer(initialChatState(0), { kind: "rejected", error });
    expect(s.phase).toBe("error");
    expect(s.error).toMatchObject({ code: "ai_quota_exhausted", resetsAt: "2026-11-19T08:00:00Z", status: 429 });
  });
});

describe("reconnect rules", () => {
  const net = new ApiError({ status: 0, code: "network_error" });
  it("reconciles (refetch, never re-POST) when the stream drops after the server accepted it", () => {
    const s = fold([{ kind: "open" }, ev({ type: "status", data: { stage: "authorizing" } }), { kind: "interrupted", error: net }]);
    expect(s.phase).toBe("interrupted");
    expect(nextStepAfterStream(s)).toBe("reconcile");
  });
  it("offers a retry only when nothing was received", () => {
    const s = fold([{ kind: "interrupted", error: net }]);
    expect(nextStepAfterStream(s)).toBe("retry");
  });
  it("does nothing after terminal states", () => {
    expect(nextStepAfterStream(fold([ev({ type: "done", data: { message_id: "m" } })]))).toBe("none");
    expect(nextStepAfterStream(fold([{ kind: "aborted" }]))).toBe("none");
  });
  it("an abort is terminal and later events are ignored", () => {
    const s = fold([{ kind: "aborted" }, ev(RESULT_EVENT)]);
    expect(s.phase).toBe("aborted");
    expect(s.result).toBeNull();
  });
});

describe("toChatEvent", () => {
  it("parses known events and rejects unknown names or bad JSON", () => {
    expect(toChatEvent("token", '{"text":"a"}')).toEqual({ type: "token", data: { text: "a" } });
    expect(toChatEvent("heartbeat", "{}")).toBeNull();
    expect(toChatEvent("sql", "{bad")).toBeNull();
    expect(toChatEvent("done", "null")).toBeNull();
  });
});

function sse(chunks: string[], { close = true, status = 200, contentType = "text/event-stream" } = {}) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch));
      if (close) c.close();
    },
  });
  return new Response(body, { status, headers: { "content-type": contentType } });
}

describe("postEventStream (one-shot POST stream)", () => {
  it("POSTs once with CSRF + Accept: text/event-stream and completes on done", async () => {
    document.cookie = "csrftoken=tok";
    const fetchImpl = vi.fn(async () => sse(['event: status\ndata: {"stage":"authorizing"}\n\n', 'event: done\ndata: {"message_id":"m1"}\n\n']));
    const seen: string[] = [];
    const out = await postEventStream({ url: "/orgs/acme/bi/conversations/c1/messages", body: { question: "q" }, fetchImpl, onMessage: (m) => seen.push(m.event) });
    expect(out).toEqual({ kind: "completed", terminal: "done", received: 2 });
    expect(seen).toEqual(["status", "done"]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/orgs/acme/bi/conversations/c1/messages");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Accept"]).toBe("text/event-stream");
    expect((init.headers as Record<string, string>)["X-CSRFToken"]).toBe("tok");
  });

  it("reports `interrupted` (and does not re-POST) when the server closes early", async () => {
    document.cookie = "csrftoken=tok";
    const fetchImpl = vi.fn(async () => sse(['event: status\ndata: {"stage":"executing"}\n\n']));
    const out = await postEventStream({ url: "/x", body: {}, fetchImpl, onMessage: () => {} });
    expect(out.kind).toBe("interrupted");
    expect(out.received).toBe(1);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("returns problem+json rejections as ApiError", async () => {
    document.cookie = "csrftoken=tok";
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ status: 403, code: "ai_policy_blocked", detail: "nope" }), { status: 403, headers: { "content-type": "application/problem+json" } }),
    );
    const out = await postEventStream({ url: "/x", body: {}, fetchImpl, onMessage: () => {} });
    expect(out.kind).toBe("rejected");
    if (out.kind === "rejected") expect(out.error.code).toBe("ai_policy_blocked");
  });

  it("hands back a JSON body when the server answers synchronously", async () => {
    document.cookie = "csrftoken=tok";
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ id: "m9", role: "assistant" }), { status: 200, headers: { "content-type": "application/json" } }));
    const out = await postEventStream({ url: "/x", body: {}, fetchImpl, onMessage: () => {} });
    expect(out).toEqual({ kind: "json", body: { id: "m9", role: "assistant" }, received: 0 });
  });

  it("reports `aborted` when the caller aborts", async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    const out = await postEventStream({ url: "/x", body: {}, fetchImpl: vi.fn(), onMessage: () => {}, signal: ctrl.signal });
    expect(out.kind).toBe("aborted");
  });
});
