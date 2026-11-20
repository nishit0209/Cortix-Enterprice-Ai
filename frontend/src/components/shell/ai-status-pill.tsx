"use client";

import * as React from "react";
import Link from "next/link";
import { ShieldCheck, Sparkles } from "lucide-react";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";
import { useAiStatus } from "@/lib/queries/org";
import { aiHealthOf, isOrgRequestScope, quotaOf } from "@/lib/ai/quota";
import { formatDuration, useCountdown } from "@/hooks/use-countdown";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/overlays";
import { Progress } from "@/components/ui/feedback";
import { POLICY_LABELS } from "@/components/ui/status";
import { Skeleton } from "@/components/ui/feedback";
import type { AiStatus } from "@/lib/types";
import { useOrgContext } from "./org-context";

/** Pill health from the W3 `/ai/status` fields (`configured`, `quota.state`, `breaker`). */
export function aiHealth(s: AiStatus | undefined): { tone: "success" | "warning" | "danger" | "neutral"; label: string } {
  return aiHealthOf(s);
}

const DANGER_OR_WARN = { exhausted: "text-danger", low: "text-warning" } as const;

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
  const quota = quotaOf(s);
  const limit = quota?.limit_today ?? s?.org_budget?.limit ?? 0;
  const used = quota?.used_today ?? s?.org_budget?.used ?? 0;
  const remaining = useCountdown(quota && quota.state !== "ok" ? quota.resets_at : null);
  // The bar is the org's request budget: colour it by the quota state only when that budget is the one that tripped.
  const barTone = !isOrgRequestScope(quota) ? "accent" : health.tone === "danger" ? "danger" : health.tone === "warning" ? "warning" : "accent";

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
            <Row label="Gemini tier">
              <span className="font-medium capitalize text-fg">{s.tier}</span>
            </Row>
            {s.models?.fast?.model && (
              <Row label="Model">
                <span className="truncate font-mono text-xs text-fg">{s.models.fast.model}</span>
              </Row>
            )}
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
                  tone={barTone}
                  label="AI budget used"
                />
              )}
            </div>
            {quota?.tokens && quota.tokens.limit > 0 && (
              <Row label="Daily tokens">
                <span className={cn("tabular font-medium", quota.scope === "org_tokens" && quota.state !== "ok" ? DANGER_OR_WARN[quota.state] : "text-fg")}>
                  {formatNumber(quota.tokens.used)} / {formatNumber(quota.tokens.limit)}
                </span>
              </Row>
            )}
            {quota?.user && quota.user.limit > 0 && (
              <Row label="Your requests today">
                <span className={cn("tabular font-medium", quota.scope === "user" && quota.state !== "ok" ? DANGER_OR_WARN[quota.state] : "text-fg")}>
                  {formatNumber(quota.user.used)} / {formatNumber(quota.user.limit)}
                </span>
              </Row>
            )}
            {quota?.scope === "platform" && quota.state !== "ok" && (
              <p className="rounded-control bg-surface-2 px-3 py-2 text-xs text-fg-muted">
                {quota.state === "exhausted" ? "The shared daily Gemini quota is used up." : "Most of the shared daily Gemini quota is used."}
              </p>
            )}
            {quota?.resets_at && (
              <Row label={quota.state === "ok" ? "Quota resets" : "Resets in"}>
                <span className="tabular text-fg" title={formatDateTime(quota.resets_at)}>
                  {quota.state !== "ok" && remaining !== null ? formatDuration(remaining) : formatDateTime(quota.resets_at)}
                </span>
              </Row>
            )}
            {s.policy_downgrade_reason && (
              <p className="rounded-control bg-warning-soft px-3 py-2 text-xs text-warning">
                The stored policy ({POLICY_LABELS[s.stored_policy ?? ""] ?? s.stored_policy}) is not in effect: {s.policy_downgrade_reason.replace(/_/g, " ")}.
              </p>
            )}
            {s.limiter_available === false && (
              <p className="rounded-control bg-surface-2 px-3 py-2 text-xs text-fg-muted">Usage is counted from the database log (the fast limiter is not running).</p>
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
