/**
 * One-shot POST → Server-Sent Events stream (chat answers, document Q&A).
 *
 * Unlike `openEventStream` (GET job streams that resume with Last-Event-ID), a POST stream is NOT
 * idempotent: re-sending it would ask the question again and spend AI budget twice. So this client
 * never reconnects on its own. It reports how the stream ended and lets the caller decide:
 *  - `completed`     a terminal event (`done` / `error`) arrived;
 *  - `rejected`      the server answered with a non-2xx status (problem+json → ApiError) before streaming;
 *  - `json`          the server answered with JSON instead of a stream (synchronous fallback);
 *  - `interrupted`   the connection dropped (or the heartbeat watchdog fired) after the server accepted it;
 *  - `aborted`       the caller aborted (Stop button / unmount).
 * See `nextStepAfterStream` in lib/chat/reducer.ts for the reconnect rules built on top of this.
 */

import { ApiError, buildUrl, ensureCsrf, toApiError } from "./api";
import { SSEParser, type SSEMessage } from "./sse";

export type PostStreamOutcome =
  | { kind: "completed"; terminal: string; received: number }
  | { kind: "rejected"; error: ApiError; received: 0 }
  | { kind: "json"; body: unknown; received: 0 }
  | { kind: "interrupted"; error: ApiError; received: number }
  | { kind: "aborted"; received: number };

export interface PostStreamOptions {
  url: string;
  body: unknown;
  onMessage: (msg: SSEMessage) => void;
  /** Called once the server accepted the request and the stream is open. */
  onOpen?: () => void;
  terminalEvents?: string[];
  /** Abort the connection when nothing (data or heartbeat) arrives for this long. */
  heartbeatTimeoutMs?: number;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  headers?: Record<string, string>;
}

export async function postEventStream(opts: PostStreamOptions): Promise<PostStreamOutcome> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const terminal = new Set(opts.terminalEvents ?? ["done", "error"]);
  const timeout = opts.heartbeatTimeoutMs ?? 60_000;
  const ctrl = new AbortController();
  const abortOuter = () => ctrl.abort();
  if (opts.signal?.aborted) return { kind: "aborted", received: 0 };
  opts.signal?.addEventListener("abort", abortOuter, { once: true });
  let watchdogFired = false;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  const arm = () => {
    if (watchdog) clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      watchdogFired = true;
      ctrl.abort();
    }, timeout);
  };
  let received = 0;

  try {
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      "Cache-Control": "no-cache",
      ...(opts.headers ?? {}),
    };
    const token = await ensureCsrf();
    if (token) headers["X-CSRFToken"] = token;
    arm();
    let res: Response;
    try {
      res = await fetchImpl(buildUrl(opts.url), {
        method: "POST",
        headers,
        body: JSON.stringify(opts.body ?? {}),
        credentials: "include",
        signal: ctrl.signal,
      });
    } catch (e) {
      if (opts.signal?.aborted) return { kind: "aborted", received: 0 };
      if ((e as Error)?.name === "AbortError" && !watchdogFired) return { kind: "aborted", received: 0 };
      return {
        kind: "rejected",
        received: 0,
        error: new ApiError({ status: 0, code: "network_error", detail: "Could not reach the NEXORA API." }),
      };
    }
    if (!res.ok) return { kind: "rejected", error: await toApiError(res), received: 0 };

    const ct = res.headers.get("content-type") ?? "";
    if (!ct.includes("text/event-stream")) {
      const text = await res.text();
      let body: unknown = text;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        /* non-JSON body: hand back the raw text */
      }
      return { kind: "json", body, received: 0 };
    }
    if (!res.body) {
      return {
        kind: "interrupted",
        received: 0,
        error: new ApiError({ status: 0, code: "stream_unsupported", detail: "This browser cannot read streamed responses." }),
      };
    }

    opts.onOpen?.();
    const parser = new SSEParser();
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch {
        if (opts.signal?.aborted) return { kind: "aborted", received };
        return {
          kind: "interrupted",
          received,
          error: new ApiError({
            status: 0,
            code: watchdogFired ? "stream_timeout" : "network_error",
            detail: watchdogFired ? "The answer stream went silent." : "The connection dropped while the answer was streaming.",
          }),
        };
      }
      if (chunk.done) break;
      arm();
      for (const msg of parser.feed(decoder.decode(chunk.value, { stream: true }))) {
        received++;
        opts.onMessage(msg);
        if (terminal.has(msg.event)) {
          ctrl.abort();
          return { kind: "completed", terminal: msg.event, received };
        }
      }
    }
    parser.end();
    if (opts.signal?.aborted) return { kind: "aborted", received };
    return {
      kind: "interrupted",
      received,
      error: new ApiError({ status: 0, code: "stream_closed", detail: "The server closed the stream before the answer finished." }),
    };
  } finally {
    if (watchdog) clearTimeout(watchdog);
    opts.signal?.removeEventListener("abort", abortOuter);
  }
}
