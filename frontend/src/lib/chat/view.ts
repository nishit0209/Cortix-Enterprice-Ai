/**
 * One view model for an assistant answer, whether it is streaming live (ChatStreamState) or persisted (Message).
 */

import type { ChartHint, ChatInsight, ChatMessage, ChatResult, ChatStage, SqlSource } from "@/lib/types";
import type { ChatError, ChatPhase, ChatStreamState } from "./reducer";

export interface AnswerView {
  key: string;
  messageId: string | null;
  phase: ChatPhase;
  stage: ChatStage | null;
  seenStages: ChatStage[];
  source: SqlSource | null;
  sql: { raw: string | null; executed: string | null; source: SqlSource } | null;
  result: ChatResult | null;
  chart: ChartHint | null;
  insights: ChatInsight[];
  assumptions: string[];
  narration: string;
  error: ChatError | null;
  durationMs: number | null;
  suggestions: string[];
  feedback: "up" | "down" | null;
  createdAt: string | null;
}

export function viewFromLive(key: string, s: ChatStreamState): AnswerView {
  return {
    key,
    messageId: s.messageId,
    phase: s.phase,
    stage: s.stage,
    seenStages: s.seenStages,
    source: s.sql?.source ?? s.source,
    sql: s.sql ? { raw: s.sql.raw, executed: s.sql.executed, source: s.sql.source } : null,
    result: s.result,
    chart: s.chart,
    insights: s.insights,
    assumptions: s.assumptions,
    narration: s.narration,
    error: s.error,
    durationMs: s.finishedAt ? s.finishedAt - s.startedAt : null,
    suggestions: [],
    feedback: null,
    createdAt: null,
  };
}

const ERROR_STATUSES = new Set(["error", "blocked", "failed", "rejected"]);

export function viewFromMessage(m: ChatMessage, live?: ChatStreamState | null): AnswerView {
  const isError = !!m.error_code || ERROR_STATUSES.has(m.status);
  const pending = m.status === "pending" || m.status === "streaming";
  const sql = m.sql ? { raw: m.sql.raw, executed: m.sql.executed, source: m.sql.source } : live?.sql ? { ...live.sql } : null;
  return {
    key: m.id,
    messageId: m.id,
    phase: isError ? "error" : pending ? "streaming" : "done",
    stage: null,
    seenStages: [],
    source: sql?.source ?? null,
    sql,
    result: m.result ?? live?.result ?? null,
    chart: m.chart ?? live?.chart ?? null,
    insights: m.insights?.length ? m.insights : (live?.insights ?? []),
    assumptions: m.assumptions?.length ? m.assumptions : (live?.assumptions ?? []),
    narration: isError ? "" : m.content || live?.narration || "",
    error: isError
      ? {
          code: m.error_code ?? live?.error?.code ?? "error",
          detail: m.error_detail ?? (m.content || live?.error?.detail || null),
          resetsAt: live?.error?.resetsAt ?? null,
          rejectCode: live?.error?.rejectCode ?? null,
        }
      : null,
    durationMs: m.duration_ms ?? null,
    suggestions: m.suggestions ?? [],
    feedback: m.feedback ?? null,
    createdAt: m.created_at,
  };
}

export function resultColumnNames(result: ChatResult | null): string[] {
  return (result?.columns ?? []).map((c) => (typeof c === "string" ? c : c.name));
}
