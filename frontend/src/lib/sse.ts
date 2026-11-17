/**
 * Server-Sent Events client.
 *
 * Implemented on fetch + ReadableStream instead of the native EventSource because:
 *  - the NEXORA job stream must resume after a page reload with an explicit `Last-Event-ID` header
 *    (EventSource can't set headers; it only resumes within the same instance), and
 *  - the chat endpoint (W3) streams from a POST, which EventSource can't do.
 * Semantics follow the WHATWG event-stream spec: `id`, `event`, `data`, `retry`, `:` comments (heartbeats).
 */

import { ApiError, buildUrl, ensureCsrf, toApiError } from "./api";

export interface SSEMessage {
  id: string | null;
  event: string;
  data: string;
}

/** Incremental, spec-compliant event-stream parser. Feed it text chunks; it returns complete messages. */
export class SSEParser {
  private buffer = "";
  private dataLines: string[] = [];
  private eventType = "";
  private hasData = false;
  /** Last `id:` seen (persists across messages, per spec). */
  lastEventId: string | null = null;
  /** Server-requested reconnection delay (ms) from `retry:`. */
  retry: number | null = null;
  /** Count of comment lines (`:hb` heartbeats). */
  comments = 0;

  feed(chunk: string): SSEMessage[] {
    this.buffer += chunk;
    const out: SSEMessage[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const ch = this.buffer[i];
      if (ch !== "\n" && ch !== "\r") continue;
      if (ch === "\r") {
        // CR at the very end may be followed by LF in the next chunk; wait for it.
        if (i === this.buffer.length - 1) break;
      }
      const line = this.buffer.slice(start, i);
      if (ch === "\r" && this.buffer[i + 1] === "\n") i++;
      start = i + 1;
      const msg = this.processLine(line);
      if (msg) out.push(msg);
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  /** Flush at end of stream (an unterminated final event is discarded, per spec). */
  end(): void {
    this.buffer = "";
    this.dataLines = [];
    this.eventType = "";
    this.hasData = false;
  }

  private processLine(line: string): SSEMessage | null {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) {
      this.comments++;
      return null;
    }
    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
    }
    switch (field) {
      case "event":
        this.eventType = value;
        break;
      case "data":
        this.dataLines.push(value);
        this.hasData = true;
        break;
      case "id":
        if (!value.includes("\0")) this.lastEventId = value;
        break;
      case "retry":
        if (/^\d+$/.test(value)) this.retry = parseInt(value, 10);
        break;
      default:
        break;
    }
    return null;
  }

  private dispatch(): SSEMessage | null {
    if (!this.hasData) {
      this.eventType = "";
      return null;
    }
    const msg: SSEMessage = {
      id: this.lastEventId,
      event: this.eventType || "message",
      data: this.dataLines.join("\n"),
    };
    this.dataLines = [];
    this.eventType = "";
    this.hasData = false;
    return msg;
  }
}

/** JSON-decode a message's data; returns null (not throw) on malformed payloads. */
export function parseEventData<T>(msg: SSEMessage): T | null {
  try {
    return JSON.parse(msg.data) as T;
  } catch {
    return null;
  }
}

export type StreamState = "idle" | "connecting" | "open" | "reconnecting" | "closed" | "error";

export interface StreamOptions {
  url: string;
  method?: "GET" | "POST";
  body?: unknown;
  lastEventId?: string | null;
  /** Events after which the stream is considered complete (no reconnect). */
  terminalEvents?: string[];
  /** Reconnect if no bytes (data or heartbeat) arrive within this window. Server heartbeats every 15s. */
  heartbeatTimeoutMs?: number;
  maxRetries?: number;
  signal?: AbortSignal;
  onMessage: (msg: SSEMessage) => void;
  onStateChange?: (state: StreamState, error?: ApiError) => void;
  /** Called whenever a new event id is observed (persist it to resume after reload). */
  onEventId?: (id: string) => void;
  /** Injected for tests. */
  fetchImpl?: typeof fetch;
  /** Injected for tests. */
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Open a resumable SSE stream. Resolves when the stream completes (terminal event, abort, or retries exhausted).
 */
export async function openEventStream(opts: StreamOptions): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const sleep = opts.sleep ?? defaultSleep;
  const terminal = new Set(opts.terminalEvents ?? ["done"]);
  const heartbeatTimeout = opts.heartbeatTimeoutMs ?? 45_000;
  const maxRetries = opts.maxRetries ?? 8;
  let lastEventId = opts.lastEventId ?? null;
  let retryDelay = 1500;
  let attempt = 0;
  let finished = false;
  let opened = false;
  let idleCloses = 0;

  while (!finished && !opts.signal?.aborted) {
    opts.onStateChange?.(attempt === 0 && !opened ? "connecting" : "reconnecting");
    const ctrl = new AbortController();
    const abortOuter = () => ctrl.abort();
    opts.signal?.addEventListener("abort", abortOuter, { once: true });
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const armWatchdog = () => {
      if (watchdog) clearTimeout(watchdog);
      watchdog = setTimeout(() => ctrl.abort(), heartbeatTimeout);
    };

    try {
      const headers: Record<string, string> = { Accept: "text/event-stream", "Cache-Control": "no-cache" };
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;
      let body: string | undefined;
      if (opts.method === "POST") {
        headers["Content-Type"] = "application/json";
        const token = await ensureCsrf();
        if (token) headers["X-CSRFToken"] = token;
        body = JSON.stringify(opts.body ?? {});
      }
      armWatchdog();
      const res = await fetchImpl(buildUrl(opts.url), {
        method: opts.method ?? "GET",
        headers,
        body,
        credentials: "include",
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const err = await toApiError(res);
        // Client errors are permanent (auth, not found); server errors are retried.
        if (res.status < 500) {
          opts.onStateChange?.("error", err);
          return;
        }
        throw err;
      }
      if (!res.body) throw new ApiError({ status: 0, code: "stream_unsupported", detail: "Streaming not supported." });

      opts.onStateChange?.("open");
      opened = true;
      attempt = 0;
      const parser = new SSEParser();
      let gotMessages = false;
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        armWatchdog();
        const messages = parser.feed(decoder.decode(value, { stream: true }));
        if (parser.retry !== null) retryDelay = parser.retry;
        for (const msg of messages) {
          gotMessages = true;
          if (msg.id && msg.id !== lastEventId) {
            lastEventId = msg.id;
            opts.onEventId?.(msg.id);
          }
          opts.onMessage(msg);
          if (terminal.has(msg.event)) finished = true;
        }
        if (finished) {
          ctrl.abort();
          break;
        }
      }
      parser.end();
      if (finished) {
        opts.onStateChange?.("closed");
        return;
      }
      // Server closed without a terminal event (e.g. max lifetime): reconnect with Last-Event-ID.
      // A connection that delivered nothing still counts against the retry budget.
      if (!gotMessages && ++idleCloses > maxRetries) {
        opts.onStateChange?.(
          "error",
          new ApiError({ status: 0, code: "network_error", detail: "The live event stream keeps closing." }),
        );
        return;
      }
    } catch (e) {
      if (opts.signal?.aborted) {
        opts.onStateChange?.("closed");
        return;
      }
      if (finished) {
        opts.onStateChange?.("closed");
        return;
      }
      attempt++;
      if (attempt > maxRetries) {
        const err =
          e instanceof ApiError
            ? e
            : new ApiError({ status: 0, code: "network_error", detail: "The live event stream disconnected." });
        opts.onStateChange?.("error", err);
        return;
      }
    } finally {
      if (watchdog) clearTimeout(watchdog);
      opts.signal?.removeEventListener("abort", abortOuter);
    }
    if (opts.signal?.aborted) break;
    await sleep(Math.min(retryDelay * Math.max(1, attempt), 15_000));
  }
  opts.onStateChange?.("closed");
}
