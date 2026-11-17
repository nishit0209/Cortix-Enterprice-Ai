"use client";

import * as React from "react";
import Link from "next/link";
import { ShieldCheck, Sparkles } from "lucide-react";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";
import { useAiStatus } from "@/lib/queries/org";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/overlays";
import { Progress } from "@/components/ui/feedback";
import { POLICY_LABELS } from "@/components/ui/status";
import { Skeleton } from "@/components/ui/feedback";
import type { AiStatus } from "@/lib/types";
import { useOrgContext } from "./org-context";

export function aiHealth(s: AiStatus | undefined): { tone: "success" | "warning" | "danger" | "neutral"; label: string } {
  if (!s) return { tone: "neutral", label: "Unknown" };
  if (!s.configured) return { tone: "neutral", label: "Not configured" };
  if (s.breaker !== "closed") return { tone: "danger", label: s.breaker === "open_day" ? "Paused for today" : "Cooling down" };
  const { limit, used } = s.org_budget ?? { limit: 0, used: 0 };
  if (limit > 0 && used >= limit) return { tone: "danger", label: "Budget used up" };
  if (limit > 0 && used / limit >= 0.8) return { tone: "warning", label: "Near budget" };
  return { tone: "success", label: "Available" };
}

const DOT = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  neutral: "bg-fg-subtle",
} as const;

export function AiStatusPill() {
  const { slug } = useOrgContext();
  const q = useAiStatus(slug);
  const s = q.data;
  const health = aiHealth(s);
  const limit = s?.org_budget?.limit ?? 0;
  const used = s?.org_budget?.used ?? 0;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex h-8 items-center gap-2 rounded-full border border-border bg-surface px-2.5 text-xs font-medium text-fg-muted shadow-xs transition-colors hover:border-border-strong hover:text-fg focus-ring"
          aria-label={`AI status: ${q.isError ? "unavailable" : health.label}`}
        >
          <Sparkles className="size-3.5 text-accent" aria-hidden />
          <span className="hidden sm:inline">AI</span>
          {q.isLoading ? (
            <Skeleton className="h-3 w-10" />
          ) : q.isError ? (
            <span className="text-fg-subtle">—</span>
          ) : (
            <>
              <span className={cn("size-1.5 rounded-full", DOT[health.tone])} aria-hidden />
              <span className="tabular hidden lg:inline">
                {formatNumber(used)}/{limit > 0 ? formatNumber(limit) : "∞"}
              </span>
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b border-border px-4 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold text-fg">Gemini AI</span>
            {!q.isError && s && (
              <span className="flex items-center gap-1.5 text-xs text-fg-muted">
                <span className={cn("size-1.5 rounded-full", DOT[health.tone])} aria-hidden />
                {health.label}
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-fg-muted">Policy, budget and availability for this organization.</p>
        </div>
        {q.isError ? (
          <div className="px-4 py-4 text-sm text-fg-muted">
            AI status isn&apos;t available yet. Data features keep working without AI.
          </div>
        ) : !s ? (
          <div className="space-y-2 px-4 py-4">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-2 w-full" />
          </div>
        ) : (
          <div className="space-y-4 px-4 py-4 text-sm">
            <Row label="Data policy">
              <span className="inline-flex items-center gap-1.5 font-medium text-fg">
                <ShieldCheck className="size-3.5 text-teal" aria-hidden />
                {POLICY_LABELS[s.policy] ?? s.policy}
              </span>
            </Row>
            <Row label="Tier">
              <span className="font-medium capitalize text-fg">{s.tier}</span>
            </Row>
            <div className="space-y-1.5">
              <Row label="Daily requests">
                <span className="tabular font-medium text-fg">
                  {formatNumber(used)} / {limit > 0 ? formatNumber(limit) : "No limit"}
                </span>
              </Row>
              {limit > 0 && (
                <Progress
                  value={used}
                  max={limit}
                  tone={health.tone === "danger" ? "danger" : health.tone === "warning" ? "warning" : "accent"}
                  label="AI budget used"
                />
              )}
            </div>
            {s.resets_at && (
              <Row label="Resets">
                <span className="text-fg">{formatDateTime(s.resets_at)}</span>
              </Row>
            )}
            {!s.configured && (
              <p className="rounded-control bg-surface-2 px-3 py-2 text-xs text-fg-muted">
                Gemini is not configured for this deployment. Cached and builder answers still work.
              </p>
            )}
          </div>
        )}
        <div className="border-t border-border px-4 py-2.5">
          <Link href={`/o/${slug}/settings/ai-policy`} className="text-xs font-medium text-accent hover:underline">
            Manage AI data policy →
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-fg-muted">{label}</span>
      {children}
    </div>
  );
}
