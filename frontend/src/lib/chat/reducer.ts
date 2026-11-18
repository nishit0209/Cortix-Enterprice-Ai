/**
 * Pure state machine for one streamed assistant answer (CONTRACT.md W3 chat SSE events).
 *
 * Event ordering rules:
 *  - `status` advances the stage stepper; a stage never moves backwards (late/duplicate events are ignored).
 *  - `sql` and `result` may arrive in either order relative to `status`; each implies its stage was reached.
 *  - `insight` events are de-duplicated by text; `token` events append to the narration.
 *  - `done` and `error` are terminal: anything after them is ignored (the stream also closes).
 *  - `error` keeps whatever SQL/result already arrived so guard rejections can show the offending SQL.
 */

import type { ApiError } from "@/lib/api";
import type { ChartHint, ChatEventMap, ChatInsight, ChatResult, ChatStage, SqlSource } from "@/lib/types";

export const CHAT_STAGES: ChatStage[] = ["authorizing", "resolving", "generating", "validating", "executing", "explaining"];

export const STAGE_LABELS: Record<ChatStage, string> = {
  authorizing: "Authorizing",
  resolving: "Resolving",
  generating: "Generating SQL",
  validating: "Validating",
  executing: "Executing",
  explaining: "Explaining",
};

export function stageIndex(stage: string | null | undefined): number {
  return stage ? CHAT_STAGES.indexOf(stage as ChatStage) : -1;
}

export type ChatPhase = "connecting" | "streaming" | "done" | "error" | "interrupted" | "aborted";

export interface ChatError {
  code: string;
  detail: string | null;
  resetsAt?: string | null;
  rejectCode?: string | null;
  requestId?: string | null;
  status?: number;
}

export interface ChatStreamState {
  phase: ChatPhase;
  stage: ChatStage | null;
  /** Stages actually reported (the stepper marks skipped ones, e.g. cache hits skip generating). */
  seenStages: ChatStage[];
  source: SqlSource | null;
  sql: { raw: string | null; executed: string | null; source: SqlSource; queryAuditId: string | null } | null;
  result: ChatResult | null;
  chart: ChartHint | null;
  insights: ChatInsight[];
  assumptions: string[];
  narration: string;
  messageId: string | null;
  error: ChatError | null;
  /** Count of events folded in (0 = nothing received yet; used by the reconnect rules). */
  received: number;
  startedAt: number;
  finishedAt: number | null;
}

export function initialChatState(now: number = Date.now()): ChatStreamState {
  return {
    phase: "connecting",
    stage: null,
    seenStages: [],
    source: null,
    sql: null,
    result: null,
    chart: null,
    insights: [],
    assumptions: [],
    narration: "",
    messageId: null,
    error: null,
    received: 0,
    startedAt: now,
    finishedAt: null,
  };
}

export type ChatEvent = { [K in keyof ChatEventMap]: { type: K; data: ChatEventMap[K] } }[keyof ChatEventMap];

export type ChatAction =
  | { kind: "open" }
  | { kind: "event"; event: ChatEvent; now?: number }
  | { kind: "rejected"; error: ApiError; now?: number }
  | { kind: "interrupted"; error: ApiError; now?: number }
  | { kind: "aborted"; now?: number };

const TERMINAL: ChatPhase[] = ["done", "error", "aborted"];

export function isTerminal(state: ChatStreamState): boolean {
  return TERMINAL.includes(state.phase);
}

function advance(state: ChatStreamState, stage: ChatStage): Pick<ChatStreamState, "stage" | "seenStages"> {
  const seen = state.seenStages.includes(stage) ? state.seenStages : [...state.seenStages, stage];
  if (stageIndex(stage) <= stageIndex(state.stage)) return { stage: state.stage, seenStages: seen };
  return { stage, seenStages: seen };
}

export function chatReducer(state: ChatStreamState, action: ChatAction): ChatStreamState {
  if (action.kind === "open") {
    return state.phase === "connecting" ? { ...state, phase: "streaming" } : state;
  }
  if (isTerminal(state)) return state;

  if (action.kind === "aborted") {
    return { ...state, phase: "aborted", finishedAt: action.now ?? Date.now() };
  }
  if (action.kind === "rejected" || action.kind === "interrupted") {
    const e = action.error;
    const body = (e.body ?? {}) as { resets_at?: string; reject_code?: string };
    return {
      ...state,
      phase: action.kind === "rejected" ? "error" : "interrupted",
      error: {
        code: e.code,
        detail: e.detail,
        status: e.status,
        requestId: e.requestId,
        resetsAt: typeof body.resets_at === "string" ? body.resets_at : null,
        rejectCode: typeof body.reject_code === "string" ? body.reject_code : null,
      },
      finishedAt: action.now ?? Date.now(),
    };
  }

  const ev = action.event;
  const base = { ...state, phase: "streaming" as ChatPhase, received: state.received + 1 };
  switch (ev.type) {
    case "status": {
      if (!CHAT_STAGES.includes(ev.data.stage)) return base;
      return { ...base, ...advance(state, ev.data.stage), source: ev.data.source ?? state.source };
    }
    case "sql": {
      const d = ev.data;
      return {
        ...base,
        ...advance(state, "validating"),
        source: d.source ?? state.source,
        sql: {
          raw: d.raw_sql ?? null,
          executed: d.executed_sql ?? null,
          source: d.source ?? state.source ?? "none",
          queryAuditId: d.query_audit_id ?? null,
        },
        assumptions: d.assumptions?.length ? dedupe([...state.assumptions, ...d.assumptions]) : state.assumptions,
      };
    }
    case "result": {
      const d = ev.data;
      return {
        ...base,
        ...advance(state, "executing"),
        result: { columns: d.columns ?? [], rows: d.rows ?? [], row_count: d.row_count ?? (d.rows ?? []).length, truncated: !!d.truncated },
        chart: d.chart ?? d.chart_spec ?? state.chart,
      };
    }
    case "insight": {
      if (!ev.data?.text || state.insights.some((i) => i.text === ev.data.text)) return base;
      return { ...base, insights: [...state.insights, { kind: ev.data.kind ?? "note", text: ev.data.text }] };
    }
    case "token":
      return { ...base, ...advance(state, "explaining"), narration: state.narration + (ev.data?.text ?? "") };
    case "done":
      return { ...base, phase: "done", messageId: ev.data?.message_id ?? null, finishedAt: action.now ?? Date.now() };
    case "error":
      return {
        ...base,
        phase: "error",
        error: {
          code: ev.data?.code || "error",
          detail: ev.data?.detail ?? null,
          resetsAt: ev.data?.resets_at ?? null,
          rejectCode: ev.data?.reject_code ?? null,
        },
        sql:
          state.sql ??
          (ev.data?.executed_sql
            ? { raw: null, executed: ev.data.executed_sql, source: state.source ?? "none", queryAuditId: null }
            : null),
        finishedAt: action.now ?? Date.now(),
      };
    default:
      return base;
  }
}

function dedupe(xs: string[]): string[] {
  return Array.from(new Set(xs));
}

/**
 * Reconnect rules for a POST chat stream (never re-POST automatically):
 *  - terminal (done/error/aborted)                → "none"
 *  - interrupted after the server accepted it     → "reconcile": refetch the conversation's messages; the backend
 *                                                   finishes and persists the answer even if the client went away.
 *  - interrupted before any event arrived         → "retry": offering the user a re-ask is safe (nothing ran).
 */
export function nextStepAfterStream(state: ChatStreamState): "none" | "reconcile" | "retry" {
  if (state.phase !== "interrupted") return "none";
  return state.received > 0 ? "reconcile" : "retry";
}

/** Parse a raw SSE message into a typed chat event (null for unknown events or malformed JSON). */
export function toChatEvent(eventName: string, raw: string): ChatEvent | null {
  const known = ["status", "sql", "result", "insight", "token", "done", "error"];
  if (!known.includes(eventName)) return null;
  try {
    const data = JSON.parse(raw);
    if (data === null || typeof data !== "object") return null;
    return { type: eventName, data } as ChatEvent;
  } catch {
    return null;
  }
}
