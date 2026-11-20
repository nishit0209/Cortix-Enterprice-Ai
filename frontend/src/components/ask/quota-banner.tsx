"use client";

import * as React from "react";
import { Hourglass, TriangleAlert } from "lucide-react";
import { formatDuration, useCountdown } from "@/hooks/use-countdown";
import { quotaBannerCopy, quotaOf } from "@/lib/ai/quota";
import type { AiStatus } from "@/lib/types";
import { cn, formatDateTime } from "@/lib/utils";

/** Live countdown to `resets_at` (re-renders every second). */
export function ResetCountdown({ resetsAt }: { resetsAt: string | null }) {
  const ms = useCountdown(resetsAt);
  if (!resetsAt || ms === null) return null;
  return (
    <span className="tabular font-medium" title={formatDateTime(resetsAt)}>
      {ms > 0 ? `Resets in ${formatDuration(ms)}` : "Resetting now…"}
    </span>
  );
}

/** Shown above the Ask AI composer when today's AI quota is low or used up (from `/ai/status`). */
export function QuotaBanner({ status, onReset }: { status: AiStatus | undefined; onReset?: () => void }) {
  const quota = quotaOf(status);
  const copy = quotaBannerCopy(quota, status?.configured !== false);
  const ms = useCountdown(copy ? (quota?.resets_at ?? null) : null);
  const fired = React.useRef(false);
  React.useEffect(() => {
    // When the countdown reaches zero, refetch the status once so the banner clears itself.
    if (ms === 0 && !fired.current) {
      fired.current = true;
      onReset?.();
    }
    if (ms !== null && ms > 0) fired.current = false;
  }, [ms, onReset]);
  if (!copy || !quota) return null;
  const Icon = copy.tone === "danger" ? Hourglass : TriangleAlert;
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="quota-banner"
      className={cn(
        "mb-2 flex items-start gap-2.5 rounded-control border px-3 py-2 text-xs",
        copy.tone === "danger" ? "border-danger/30 bg-danger-soft/60 text-fg" : "border-warning/30 bg-warning-soft/60 text-fg",
      )}
    >
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", copy.tone === "danger" ? "text-danger" : "text-warning")} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2">
          <span className="font-semibold">{copy.title}</span>
          <ResetCountdown resetsAt={quota.resets_at} />
        </div>
        <p className="text-fg-muted">{copy.text}</p>
      </div>
    </div>
  );
}
