"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { useEventStream, type TypedEvent } from "./use-event-stream";
import { useJob } from "@/lib/queries/datasets";
import type { Job, JobEventMap, JobStatus } from "@/lib/types";

export interface JobLogLine {
  level: "info" | "warning" | "error";
  message: string;
  step_key?: string;
  at: number;
}

export interface JobProgressState {
  status: JobStatus | "connecting";
  progress: number | null;
  message: string | null;
  logs: JobLogLine[];
  result: Record<string, unknown> | null;
  error: { code: string; message?: string } | null;
  done: boolean;
}

const initial: JobProgressState = {
  status: "connecting",
  progress: null,
  message: null,
  logs: [],
  result: null,
  error: null,
  done: false,
};

type Action = { kind: "event"; event: TypedEvent<JobEventMap> } | { kind: "snapshot"; job: Job } | { kind: "reset" };

/** Pure reducer: folds job SSE events (and a REST snapshot) into UI state. Exported for tests. */
export function jobReducer(state: JobProgressState, action: Action): JobProgressState {
  if (action.kind === "reset") return initial;
  if (action.kind === "snapshot") {
    const j = action.job;
    const terminal = j.status === "succeeded" || j.status === "failed" || j.status === "cancelled";
    return {
      ...state,
      status: j.status,
      progress: j.progress ?? state.progress,
      message: j.message ?? state.message,
      result: j.result ?? state.result,
      error: j.error ? { code: j.error.code, message: j.error.message } : state.error,
      done: state.done || terminal,
    };
  }
  const ev = action.event;
  switch (ev.type) {
    case "status":
      return {
        ...state,
        status: ev.data.status ?? (state.status === "connecting" ? "running" : state.status),
        progress: ev.data.progress ?? state.progress,
        message: ev.data.message ?? state.message,
      };
    case "log":
      return {
        ...state,
        logs: [...state.logs.slice(-199), { ...ev.data, at: Date.now() }],
      };
    case "result":
      return { ...state, result: ev.data.result ?? ev.data.result_ref ?? null, progress: 100 };
    case "error":
      return {
        ...state,
        status: "failed",
        error: { code: ev.data.code ?? "job_failed", message: ev.data.message ?? (typeof ev.data.detail === "string" ? ev.data.detail : undefined) },
      };
    case "done":
      return {
        ...state,
        status: ev.data.status ?? state.status,
        progress: ev.data.status === "succeeded" ? 100 : state.progress,
        done: true,
      };
    default:
      return state;
  }
}

/**
 * Live job progress: REST snapshot (GET /jobs/{id}) + SSE stream (/jobs/{id}/events) with Last-Event-ID resume.
 */
export function useJobStream(
  org: string,
  jobId: string | null,
  options: { onDone?: (state: JobProgressState) => void } = {},
) {
  const [state, dispatch] = useReducer(jobReducer, initial);
  const snapshot = useJob(org, jobId);
  const onDoneRef = useRef(options.onDone);
  onDoneRef.current = options.onDone;
  const firedRef = useRef<string | null>(null);

  useEffect(() => {
    dispatch({ kind: "reset" });
    firedRef.current = null;
  }, [jobId]);

  useEffect(() => {
    if (snapshot.data) dispatch({ kind: "snapshot", job: snapshot.data });
  }, [snapshot.data]);

  const onEvent = useCallback((event: TypedEvent<JobEventMap>) => dispatch({ kind: "event", event }), []);
  const url = jobId ? `/orgs/${encodeURIComponent(org)}/jobs/${jobId}/events` : null;
  const stream = useEventStream<JobEventMap>(state.done ? null : url, {
    onEvent,
    terminalEvents: ["done"],
    resumeKey: jobId ? `job:${jobId}` : undefined,
  });

  useEffect(() => {
    if (state.done && jobId && firedRef.current !== jobId) {
      firedRef.current = jobId;
      onDoneRef.current?.(state);
    }
  }, [state, jobId]);

  return { ...state, connection: stream.state, streamError: stream.error };
}
