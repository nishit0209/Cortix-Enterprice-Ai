"use client";

import * as React from "react";
import Link from "next/link";
import { ShieldCheck, ShieldOff, ShieldQuestion, Sparkles } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/overlays";
import { Progress } from "@/components/ui/feedback";
import { ClassificationBadge, POLICY_LABELS } from "@/components/ui/status";
import { aiHealth } from "@/components/shell/ai-status-pill";
import type { AiStatus, Dataset } from "@/lib/types";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";

export type AiEligibility = "values" | "schema" | "none" | "unknown";

/** What Gemini may receive for this dataset under the effective policy (mirrors AI_ARCHITECTURE §5). */
export function aiEligibility(policy: string | null | undefined, classification: string | null | undefined): AiEligibility {
  if (!policy || !classification) return "unknown";
  const open = classification === "synthetic" || classification === "public";
  if (open) return "values";
  if (policy === "schema_only") return "schema";
  if (policy === "aggregates_only" || policy === "full") return classification === "confidential" ? "schema" : "values";
  return "none";
}

const ELIGIBILITY_COPY: Record<AiEligibility, { label: string; text: string }> = {
  values: { label: "AI on", text: "Gemini may generate SQL and narrate results for this dataset." },
  schema: { label: "Schema only", text: "Gemini sees column names and types only; results are explained locally." },
  none: {
    label: "AI off for this dataset",
    text: "Cached and builder answers still work. Questions that need Gemini will be blocked by the AI data policy.",
  },
  unknown: { label: "AI policy", text: "Policy status is loading or unavailable." },
};

/** Header chip: effective policy + eligibility of the current dataset + budget (popover with details). */
export function AiContextChip({ slug, status, dataset }: { slug: string; status: AiStatus | undefined; dataset: Dataset | undefined }) {
  const elig = aiEligibility(status?.policy, dataset?.classification);
  const health = aiHealth(status);
  const copy = ELIGIBILITY_COPY[elig];
  const Icon = elig === "values" ? ShieldCheck : elig === "none" ? ShieldOff : ShieldQuestion;
  const budget = status?.org_budget;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex h-8 items-center gap-2 rounded-full border px-3 text-xs font-medium transition-colors focus-ring",
            elig === "values"
              ? "border-teal/30 bg-teal-soft text-teal"
              : elig === "none"
                ? "border-warning/30 bg-warning-soft text-warning"
                : "border-border bg-surface text-fg-muted",
          )}
          aria-label={`AI for this dataset: ${copy.label}`}
        >
          <Icon className="size-3.5" aria-hidden />
          <span className="hidden sm:inline">{copy.label}</span>
          {budget && budget.limit > 0 && (
            <span className="tabular hidden border-l border-current/20 pl-2 opacity-80 md:inline">
              {formatNumber(budget.used)}/{formatNumber(budget.limit)}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-accent" aria-hidden />
          <span className="text-sm font-semibold text-fg">AI for this conversation</span>
        </div>
        <p className="text-sm text-fg-muted">{copy.text}</p>
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-sm">
          <dt className="text-fg-muted">Policy</dt>
          <dd className="text-right font-medium text-fg">{status ? (POLICY_LABELS[status.policy] ?? status.policy) : "—"}</dd>
          <dt className="text-fg-muted">Dataset</dt>
          <dd className="flex justify-end">{dataset ? <ClassificationBadge value={dataset.classification} /> : "—"}</dd>
          <dt className="text-fg-muted">Gemini</dt>
          <dd className="text-right text-fg">{status ? health.label : "—"}</dd>
        </dl>
        {budget && budget.limit > 0 && (
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-fg-muted">
              <span>Daily requests</span>
              <span className="tabular">
                {formatNumber(budget.used)} / {formatNumber(budget.limit)}
              </span>
            </div>
            <Progress value={budget.used} max={budget.limit} tone={health.tone === "danger" ? "danger" : health.tone === "warning" ? "warning" : "accent"} label="AI budget used" />
            {(status?.budget_resets_at ?? status?.resets_at) && (
              <p className="text-[11px] text-fg-subtle">Resets {formatDateTime(status?.budget_resets_at ?? status?.resets_at)}</p>
            )}
          </div>
        )}
        <Link href={`/o/${slug}/settings/ai-policy`} className="block text-xs font-medium text-accent hover:underline">
          Manage AI data policy →
        </Link>
      </PopoverContent>
    </Popover>
  );
}
