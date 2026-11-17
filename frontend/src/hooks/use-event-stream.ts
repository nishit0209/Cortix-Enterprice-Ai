"use client";

import { useEffect, useRef, useState } from "react";
import type { ApiError } from "@/lib/api";
import { openEventStream, parseEventData, type SSEMessage, type StreamState } from "@/lib/sse";

/** A typed event: `{ type: "status", data: {...} }` discriminated by the event map's keys. */
export type TypedEvent<M> = { [K in keyof M & string]: { type: K; id: string | null; data: M[K] } }[keyof M & string];

export interface UseEventStreamOptions<M> {
  /** Called for every event with JSON-decoded data. Unknown/malformed events are skipped. */
  onEvent?: (event: TypedEvent<M>) => void;
  terminalEvents?: Array<keyof M & string>;
  /**
   * Persist the last event id in sessionStorage under this key so a page reload resumes
   * the stream with `Last-Event-ID` instead of replaying everything.
   */
  resumeKey?: string;
  enabled?: boolean;
  /** Injected for tests. */
  fetchImpl?: typeof fetch;
}

function readResume(key?: string): string | null {
  if (!key || typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(`nexora:sse:${key}`);
  } catch {
    return null;
  }
}

function writeResume(key: string | undefined, id: string | null) {
  if (!key || typeof window === "undefined") return;
  try {
    if (id) window.sessionStorage.setItem(`nexora:sse:${key}`, id);
    else window.sessionStorage.removeItem(`nexora:sse:${key}`);
  } catch {
    /* storage unavailable: resume is best-effort */
  }
}

export function useEventStream<M extends object>(url: string | null, options: UseEventStreamOptions<M> = {}) {
  const [state, setState] = useState<StreamState>("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const [lastEventId, setLastEventId] = useState<string | null>(null);
  const onEventRef = useRef(options.onEvent);
  onEventRef.current = options.onEvent;
  const { resumeKey, enabled = true, fetchImpl } = options;
  const terminalKey = (options.terminalEvents ?? ["done"]).join(",");

  useEffect(() => {
    if (!url || !enabled) return;
    const ctrl = new AbortController();
    const terminal = terminalKey.split(",");
    setError(null);
    void openEventStream({
      url,
      lastEventId: readResume(resumeKey),
      terminalEvents: terminal,
      signal: ctrl.signal,
      fetchImpl,
      onEventId: (id) => {
        setLastEventId(id);
        writeResume(resumeKey, id);
      },
      onStateChange: (s, err) => {
        if (ctrl.signal.aborted) return;
        setState(s);
        if (err) setError(err);
      },
      onMessage: (msg: SSEMessage) => {
        const data = parseEventData<unknown>(msg);
        if (data === null && msg.data !== "null") return;
        onEventRef.current?.({ type: msg.event, id: msg.id, data } as TypedEvent<M>);
        if (terminal.includes(msg.event)) writeResume(resumeKey, null);
      },
    });
    return () => ctrl.abort();
  }, [url, enabled, resumeKey, terminalKey, fetchImpl]);

  return { state, error, lastEventId };
}
