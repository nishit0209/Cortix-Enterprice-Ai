"use client";

import * as React from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Progress } from "@/components/ui/feedback";
import { useJobStream, type JobProgressState } from "@/hooks/use-job-stream";
import { describeError } from "@/lib/errors";
import { cn } from "@/lib/utils";

/** Live job progress (REST snapshot + SSE with Last-Event-ID resume): bar, status line and streamed log. */
export function JobProgress({
  org,
  jobId,
  onDone,
  title,
  showLogs = true,
  className,
}: {
  org: string;
  jobId: string;
  onDone?: (state: JobProgressState) => void;
  title?: string;
  showLogs?: boolean;
  className?: string;
}) {
  const job = useJobStream(org, jobId, { onDone });
  const failed = job.status === "failed" || job.status === "cancelled";
  const succeeded = job.done && job.status === "succeeded";
  const logRef = React.useRef<HTMLOListElement>(null);
  React.useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [job.logs.length]);
  return (
    <div className={cn("space-y-2.5", className)} aria-live="polite">
      <div className="flex items-center gap-2 text-sm">
        {succeeded ? (
          <CheckCircle2 className="size-4 text-success" aria-hidden />
        ) : failed ? (
          <XCircle className="size-4 text-danger" aria-hidden />
        ) : (
          <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
        )}
        <span className="font-medium text-fg">{title ?? "Job"}</span>
        <span className="truncate text-fg-muted">
          {failed
            ? (job.error?.message ?? describeError({}).title)
            : succeeded
              ? "Done"
              : (job.message ?? (job.status === "connecting" ? "Connecting…" : job.status))}
        </span>
        {job.progress !== null && !failed && <span className="tabular ml-auto text-xs text-fg-subtle">{Math.round(job.progress)}%</span>}
      </div>
      <Progress
        value={succeeded ? 100 : (job.progress ?? 0)}
        indeterminate={!succeeded && !failed && job.progress === null}
        tone={failed ? "danger" : succeeded ? "success" : "accent"}
        label={`${title ?? "Job"} progress`}
      />
      {failed && job.error?.code && <p className="font-mono text-xs text-danger">{job.error.code}</p>}
      {job.connection === "error" && job.streamError && !job.done && (
        <p className="text-xs text-warning">Live updates disconnected ({job.streamError.code}). Progress refreshes when you reopen this view.</p>
      )}
      {showLogs && job.logs.length > 0 && (
        <ol ref={logRef} className="scrollbar-thin max-h-40 space-y-0.5 overflow-y-auto rounded-control bg-surface-2 px-3 py-2 font-mono text-[11.5px] leading-5">
          {job.logs.map((l, i) => (
            <li key={i} className={cn(l.level === "error" ? "text-danger" : l.level === "warning" ? "text-warning" : "text-fg-muted")}>
              {l.step_key && <span className="text-fg-subtle">[{l.step_key}] </span>}
              {l.message}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
