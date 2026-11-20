"use client";

import * as React from "react";
import { ArrowUp, BookOpenCheck, FileSearch, Loader2, Quote, ShieldCheck, Sparkles, Square } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Tooltip } from "@/components/ui/overlays";
import { docAnswerReducer, initialDocAnswer } from "@/lib/documents/answer";
import { highlightTerms, type TextSegment } from "@/lib/queries/documents";
import { postEventStream } from "@/lib/post-stream";
import { parseEventData } from "@/lib/sse";
import { chatErrorCopy } from "@/lib/chat/error-copy";
import type { DocumentAnswer, DocumentCitation } from "@/lib/types";
import { cn } from "@/lib/utils";

export function Highlighted({ segments, className }: { segments: TextSegment[]; className?: string }) {
  return (
    <span className={className}>
      {segments.map((s, i) =>
        s.match ? (
          <mark key={i} className="rounded-[3px] bg-accent-soft px-0.5 text-accent-soft-fg">
            {s.text}
          </mark>
        ) : (
          <React.Fragment key={i}>{s.text}</React.Fragment>
        ),
      )}
    </span>
  );
}

/** Why an answer is extractive (`reason` from POST /documents/ask). */
export const EXTRACTIVE_REASONS: Record<string, { label: string; text: string }> = {
  ai_policy_blocked: { label: "extractive (policy)", text: "Your AI data policy doesn't allow sending these documents to Gemini, so the answer quotes the most relevant passages instead." },
  ai_not_configured: { label: "extractive", text: "Gemini isn't configured on this deployment, so the answer quotes the most relevant passages." },
  ai_quota_exhausted: { label: "extractive (quota)", text: "Today's AI quota is used up, so the answer quotes the most relevant passages." },
  ai_rate_limited: { label: "extractive (busy)", text: "Gemini is rate-limited right now, so the answer quotes the most relevant passages." },
  ai_unavailable: { label: "extractive", text: "Gemini didn't respond, so the answer quotes the most relevant passages." },
  ai_invalid_output: { label: "extractive", text: "The AI answer couldn't be verified, so the answer quotes the most relevant passages." },
  no_valid_citations: { label: "extractive", text: "The AI answer had no verifiable citation, so the answer quotes the passages directly." },
  no_relevant_passages: { label: "no match", text: "No passage in the selected documents matches the question." },
};

/**
 * Ask-your-documents panel: streamed answer with citation chips (click → page). When the AI data policy does not
 * allow document text to reach Gemini the backend answers extractively, which is shown with an "extractive (policy)" badge.
 */
export function DocumentAskPanel({
  org,
  documentIds,
  onCitation,
  title = "Ask your documents",
  compact,
}: {
  org: string;
  documentIds?: string[];
  onCitation: (c: DocumentCitation) => void;
  title?: string;
  compact?: boolean;
}) {
  const [question, setQuestion] = React.useState("");
  const [asked, setAsked] = React.useState<string | null>(null);
  const [state, dispatch] = React.useReducer(docAnswerReducer, initialDocAnswer);
  const ctrl = React.useRef<AbortController | null>(null);
  React.useEffect(() => () => ctrl.current?.abort(), []);

  const ask = async () => {
    const q = question.trim();
    if (!q || state.phase === "streaming") return;
    ctrl.current?.abort();
    const c = new AbortController();
    ctrl.current = c;
    setAsked(q);
    dispatch({ kind: "start" });
    const out = await postEventStream({
      url: `/orgs/${encodeURIComponent(org)}/documents/ask`,
      body: { question: q, ...(documentIds?.length ? { document_ids: documentIds } : {}) },
      signal: c.signal,
      onMessage: (m) => {
        const data = parseEventData<Record<string, unknown>>(m);
        if (data && typeof data === "object") dispatch({ kind: "event", event: m.event, data });
      },
    });
    if (out.kind === "json") dispatch({ kind: "json", body: (out.body ?? {}) as Partial<DocumentAnswer> });
    else if (out.kind === "rejected" || out.kind === "interrupted") dispatch({ kind: "fail", code: out.error.code, detail: out.error.detail });
    else if (out.kind === "aborted") dispatch({ kind: "fail", code: "aborted", detail: "Stopped." });
    else if (out.kind === "completed" && out.terminal === "done") dispatch({ kind: "event", event: "done", data: {} });
  };

  const streaming = state.phase === "streaming";
  const extractive = state.mode === "extractive";
  const err = state.error ? chatErrorCopy(state.error.code, state.error.detail) : null;

  return (
    <Card className={cn("relative overflow-hidden", compact && "shadow-none")}>
      <div className="pointer-events-none absolute inset-x-0 top-0 h-20 opacity-60" style={{ background: "linear-gradient(180deg, var(--accent-soft) 0%, transparent 100%)" }} aria-hidden />
      <CardHeader className="relative" icon={<Sparkles />} title={title} description="Answers cite the exact pages they come from." />
      <CardContent className="relative space-y-3">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask();
          }}
          className="flex items-end gap-2 rounded-[12px] border border-border bg-surface p-1.5 pl-3 focus-within:border-accent focus-within:ring-3 focus-within:ring-accent/15"
        >
          <label htmlFor="doc-ask" className="sr-only">
            Question about your documents
          </label>
          <input
            id="doc-ask"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="What is the notice period in the supplier contract?"
            className="h-8 min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-fg-subtle"
            maxLength={1000}
          />
          {streaming ? (
            <Button type="button" size="icon-sm" variant="secondary" onClick={() => ctrl.current?.abort()} aria-label="Stop">
              <Square className="fill-current" />
            </Button>
          ) : (
            <Button type="submit" size="icon-sm" disabled={!question.trim()} aria-label="Ask">
              <ArrowUp />
            </Button>
          )}
        </form>

        {asked && (
          <div className="space-y-3" aria-live="polite" aria-busy={streaming}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-fg-muted">Q: {asked}</span>
              {state.mode && (
                extractive ? (
                  <Tooltip content={state.message ?? EXTRACTIVE_REASONS[state.reason ?? ""]?.text ?? EXTRACTIVE_REASONS.ai_policy_blocked.text}>
                    <span>
                      <Badge tone="teal">
                        <ShieldCheck /> {EXTRACTIVE_REASONS[state.reason ?? ""]?.label ?? "extractive"}
                      </Badge>
                    </span>
                  </Tooltip>
                ) : (
                  <Badge tone="accent">
                    <Sparkles /> generative
                  </Badge>
                )
              )}
            </div>
            {streaming && !state.text && (
              <p className="flex items-center gap-2 text-sm text-fg-muted">
                <Loader2 className="size-4 animate-spin text-accent" aria-hidden /> {state.stage ? `${state.stage}…` : "Searching passages…"}
              </p>
            )}
            {err && state.error?.code !== "aborted" && (
              <div role="alert" className="rounded-control border border-danger/30 bg-danger-soft/60 px-3 py-2 text-sm">
                <div className="font-medium text-fg">{err.title}</div>
                <div className="text-fg-muted">{err.description}</div>
              </div>
            )}
            {state.notFound && !streaming ? (
              <p className="flex items-center gap-2 rounded-control bg-surface-2 px-3 py-2 text-sm text-fg-muted">
                <FileSearch className="size-4" aria-hidden /> Not found in your documents.
              </p>
            ) : (
              state.text && (
                <div className={cn("whitespace-pre-wrap text-sm leading-6 text-fg", extractive && "border-l-2 border-teal/50 pl-3")}>
                  {extractive && <Quote className="mb-1 size-3.5 text-teal" aria-hidden />}
                  <Highlighted segments={highlightTerms(state.text, extractive ? asked : "")} />
                  {streaming && <span className="ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 animate-pulse rounded-sm bg-accent" aria-hidden />}
                </div>
              )
            )}
            {state.citations.length > 0 && (
              <div className="space-y-1.5">
                <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-subtle">Sources</div>
                <ul className="flex flex-wrap gap-1.5">
                  {state.citations.map((c, i) => (
                    <li key={`${c.document_id}:${c.page}:${i}`}>
                      <Tooltip content={c.text ? `${c.text.slice(0, 220)}${c.text.length > 220 ? "…" : ""}` : `Page ${c.page}`}>
                        <button
                          type="button"
                          onClick={() => onCitation(c)}
                          className="inline-flex max-w-[260px] items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-fg transition-colors hover:border-accent/40 hover:bg-accent-soft focus-ring"
                        >
                          <BookOpenCheck className="size-3 shrink-0 text-accent" aria-hidden />
                          <span className="font-semibold">[{i + 1}]</span>
                          <span className="truncate">{c.document_title ?? "Document"}</span>
                          <span className="shrink-0 text-fg-subtle">p.{c.page}</span>
                        </button>
                      </Tooltip>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
