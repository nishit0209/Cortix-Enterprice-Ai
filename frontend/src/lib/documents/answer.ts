/**
 * Document Q&A answer state. The ask endpoint streams SSE (or answers with JSON). Tolerated events:
 *   status {stage} · citations {citations:[...]} · citation {...} · mode {mode, reason}
 *   token {text} · answer {answer, mode, citations, reason, not_found} · done {...} · error {code, detail}
 * `mode: "extractive"` means the AI data policy did not allow sending document text to Gemini, so the answer
 * is assembled from verified passages only.
 */

import type { DocumentAnswer, DocumentCitation } from "@/lib/types";

export interface DocAnswerState {
  phase: "idle" | "streaming" | "done" | "error";
  stage: string | null;
  text: string;
  mode: string | null;
  reason: string | null;
  citations: DocumentCitation[];
  notFound: boolean;
  error: { code: string; detail: string | null } | null;
}

export const initialDocAnswer: DocAnswerState = {
  phase: "idle",
  stage: null,
  text: "",
  mode: null,
  reason: null,
  citations: [],
  notFound: false,
  error: null,
};

function normCitation(c: Record<string, unknown>): DocumentCitation | null {
  const document_id = String(c.document_id ?? c.doc_id ?? "");
  const page = Number(c.page ?? c.page_no ?? c.page_from ?? NaN);
  if (!document_id || Number.isNaN(page)) return null;
  return {
    document_id,
    page,
    document_title: typeof c.document_title === "string" ? c.document_title : typeof c.title === "string" ? c.title : undefined,
    chunk_id: typeof c.chunk_id === "string" ? c.chunk_id : undefined,
    text: typeof c.text === "string" ? c.text : typeof c.snippet === "string" ? c.snippet : undefined,
  };
}

function mergeCitations(a: DocumentCitation[], b: unknown): DocumentCitation[] {
  const list = (Array.isArray(b) ? b : [b]).map((c) => (c && typeof c === "object" ? normCitation(c as Record<string, unknown>) : null)).filter(Boolean) as DocumentCitation[];
  const key = (c: DocumentCitation) => `${c.document_id}:${c.page}:${c.chunk_id ?? ""}`;
  const seen = new Set(a.map(key));
  return [...a, ...list.filter((c) => !seen.has(key(c)) && seen.add(key(c)))];
}

export type DocAnswerAction =
  | { kind: "start" }
  | { kind: "event"; event: string; data: Record<string, unknown> }
  | { kind: "json"; body: Partial<DocumentAnswer> }
  | { kind: "fail"; code: string; detail: string | null };

export function docAnswerReducer(s: DocAnswerState, a: DocAnswerAction): DocAnswerState {
  if (a.kind === "start") return { ...initialDocAnswer, phase: "streaming" };
  if (s.phase === "done" || s.phase === "error") return s;
  if (a.kind === "fail") return { ...s, phase: "error", error: { code: a.code, detail: a.detail } };
  if (a.kind === "json") {
    const b = a.body;
    return {
      ...s,
      phase: "done",
      text: b.answer ?? s.text,
      mode: b.mode ?? s.mode,
      reason: b.reason ?? s.reason,
      notFound: !!b.not_found,
      citations: mergeCitations(s.citations, b.citations ?? []),
    };
  }
  const d = a.data;
  switch (a.event) {
    case "status":
      return { ...s, stage: typeof d.stage === "string" ? d.stage : s.stage, mode: typeof d.mode === "string" ? d.mode : s.mode };
    case "mode":
      return { ...s, mode: String(d.mode ?? s.mode), reason: typeof d.reason === "string" ? d.reason : s.reason };
    case "citation":
      return { ...s, citations: mergeCitations(s.citations, d) };
    case "citations":
      return { ...s, citations: mergeCitations(s.citations, d.citations ?? []) };
    case "token":
      return { ...s, text: s.text + String(d.text ?? "") };
    case "answer":
      return {
        ...s,
        text: typeof d.answer === "string" ? d.answer : s.text,
        mode: typeof d.mode === "string" ? d.mode : s.mode,
        reason: typeof d.reason === "string" ? d.reason : s.reason,
        notFound: d.not_found === true || s.notFound,
        citations: mergeCitations(s.citations, d.citations ?? []),
      };
    case "done":
      return { ...s, phase: "done", notFound: d.not_found === true || s.notFound };
    case "error":
      return { ...s, phase: "error", error: { code: String(d.code ?? "error"), detail: typeof d.detail === "string" ? d.detail : null } };
    default:
      return s;
  }
}
