"use client";

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, Clock, KeyRound, RotateCw, ShieldAlert, ShieldX, TimerOff, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ClassificationBadge, POLICY_LABELS } from "@/components/ui/status";
import { chatErrorCopy } from "@/lib/chat/error-copy";
import type { ChatError } from "@/lib/chat/reducer";
import { formatDuration, useCountdown } from "@/hooks/use-countdown";
import { cn, formatDateTime } from "@/lib/utils";
import { SqlPanel } from "./sql-code";

export interface ErrorCardContext {
  slug: string;
  policy?: string | null;
  datasetId?: string | null;
  classification?: string | null;
  aiResetsAt?: string | null;
  canManagePolicy?: boolean;
}

/** Error card per error code: policy, quota (with countdown), not configured, SQL guard rejects (with the SQL). */
export function ChatErrorCard({
  error,
  sql,
  sqlSource,
  editorHref,
  ctx,
  onRetry,
}: {
  error: ChatError;
  sql?: { raw: string | null; executed: string | null } | null;
  sqlSource?: string | null;
  editorHref?: string | null;
  ctx: ErrorCardContext;
  onRetry?: () => void;
}) {
  const copy = chatErrorCopy(error.code, error.detail, error.rejectCode);
  const resetsAt = error.resetsAt ?? (copy.kind === "quota" ? ctx.aiResetsAt : null);
  const remaining = useCountdown(resetsAt);
  const Icon =
    copy.kind === "policy"
      ? ShieldX
      : copy.kind === "quota"
        ? Clock
        : copy.kind === "not_configured"
          ? KeyRound
          : copy.kind === "guard"
            ? ShieldAlert
            : copy.kind === "timeout"
              ? TimerOff
              : error.code === "network_error" || error.code === "stream_closed"
                ? WifiOff
                : AlertTriangle;
  const tone = copy.kind === "quota" || copy.kind === "not_configured" ? "warning" : "danger";
  const detail = error.detail && error.detail !== copy.description && copy.kind === "guard" ? error.detail : null;

  return (
    <div
      role="alert"
      className={cn(
        "space-y-3 rounded-card border px-4 py-3.5",
        tone === "warning" ? "border-warning/30 bg-warning-soft/50" : "border-danger/30 bg-danger-soft/50",
      )}
    >
      <div className="flex gap-3">
        <span
          className={cn(
            "mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-control",
            tone === "warning" ? "bg-warning-soft text-warning" : "bg-danger-soft text-danger",
          )}
        >
          <Icon className="size-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="text-sm font-semibold text-fg">{copy.title}</h4>
            <Badge tone="outline" className="font-mono">
              {error.rejectCode ?? error.code}
            </Badge>
          </div>
          <p className="text-sm text-fg-muted">{copy.description}</p>
          {detail && <p className="text-xs text-fg-subtle">{detail}</p>}

          {copy.kind === "policy" && (
            <div className="space-y-2 pt-1 text-sm text-fg-muted">
              <p className="flex flex-wrap items-center gap-1.5">
                {ctx.policy && (
                  <>
                    Your organization uses <Badge tone="teal">{POLICY_LABELS[ctx.policy] ?? ctx.policy}</Badge>
                  </>
                )}
                {ctx.classification && (
                  <>
                    and this dataset is classified <ClassificationBadge value={ctx.classification} />.
                  </>
                )}
              </p>
              <p className="text-xs">
                Only datasets attested as <strong>synthetic</strong> or <strong>public</strong> can be sent to Gemini under synthetic_only.
                Owners and admins can consent to <strong>schema_only</strong>, which shares column names and types but never values.
              </p>
              <div className="flex flex-wrap gap-2 pt-1">
                <Button size="xs" variant="secondary" asChild>
                  <Link href={`/o/${ctx.slug}/settings/ai-policy`}>
                    <ShieldAlert /> {ctx.canManagePolicy ? "Review AI policy" : "View AI policy"}
                  </Link>
                </Button>
                {ctx.datasetId && (
                  <Button size="xs" variant="ghost" asChild>
                    <Link href={`/o/${ctx.slug}/data/datasets/${ctx.datasetId}`}>Dataset classification</Link>
                  </Button>
                )}
              </div>
            </div>
          )}

          {copy.kind === "quota" && (
            <p className="flex flex-wrap items-center gap-2 pt-1 text-sm">
              {remaining !== null && remaining > 0 ? (
                <>
                  <span className="text-fg-muted">Resets in</span>
                  <span className="tabular rounded-control bg-surface px-2 py-0.5 font-mono text-sm font-semibold text-fg" aria-live="off">
                    {formatDuration(remaining)}
                  </span>
                  <span className="text-xs text-fg-subtle">({formatDateTime(resetsAt)})</span>
                </>
              ) : remaining === 0 ? (
                <span className="text-fg-muted">The budget has reset. You can ask again.</span>
              ) : (
                <span className="text-fg-muted">The budget resets automatically at midnight Pacific time.</span>
              )}
            </p>
          )}

          {copy.kind === "not_configured" && (
            <p className="pt-1 text-xs text-fg-subtle">
              An administrator can set <code className="font-mono">GEMINI_API_KEY</code> on the backend to enable AI-generated SQL.
            </p>
          )}

          {(error.code === "network_error" || error.code === "stream_closed" || error.code === "stream_timeout") && (
            <p className="pt-1 text-xs text-fg-subtle">
              The answer may still finish on the server; it will appear here when it does.
            </p>
          )}
          {error.requestId && <p className="pt-1 font-mono text-[11px] text-fg-subtle">request {error.requestId}</p>}
        </div>
        {onRetry && (
          <Button variant="secondary" size="xs" onClick={onRetry} className="self-start">
            <RotateCw /> Retry
          </Button>
        )}
      </div>
      {copy.showSql && (sql?.executed || sql?.raw) && (
        <SqlPanel executed={sql.executed} raw={sql.raw} source={sqlSource} editorHref={editorHref} defaultOpen tone="danger" />
      )}
    </div>
  );
}
