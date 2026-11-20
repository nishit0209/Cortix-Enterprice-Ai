"use client";

import * as React from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, History, RotateCw, Rows3, Timer, TriangleAlert, Workflow } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { ScoreRing } from "@/components/ui/score-ring";
import { StatusBadge } from "@/components/ui/status";
import { Tooltip } from "@/components/ui/overlays";
import { JobProgress } from "@/components/jobs/job-progress";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { usePipeline, usePipelineRun, usePipelineRuns, useRetryRun, useRunLogs } from "@/lib/queries/pipelines";
import { formatMs } from "@/hooks/use-countdown";
import type { PipelineRun, PipelineRunStep } from "@/lib/types";
import { cn, formatDateTime, formatNumber, formatRelative, titleCase } from "@/lib/utils";

const LIVE = new Set(["queued", "running", "retrying"]);

export function RunsView() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const router = useRouter();
  const pipeline = usePipeline(slug, id);
  const runs = usePipelineRuns(slug, id);
  useSetBreadcrumb(pipeline.data?.name ? `${pipeline.data.name} · runs` : null);

  const columns = React.useMemo<DataTableColumn<PipelineRun>[]>(
    () => [
      { id: "status", accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
      {
        id: "started_at",
        accessorFn: (r) => r.started_at ?? r.created_at ?? "",
        header: "Started",
        cell: ({ row }) => (
          <Tooltip content={formatDateTime(row.original.started_at ?? row.original.created_at)}>
            <span className="whitespace-nowrap">{formatRelative(row.original.started_at ?? row.original.created_at)}</span>
          </Tooltip>
        ),
      },
      { id: "trigger", accessorFn: (r) => r.trigger ?? "manual", header: "Trigger", cell: ({ row }) => titleCase(row.original.trigger ?? "manual") },
      { id: "attempt", accessorKey: "attempt", header: "Attempt", meta: { align: "right" } },
      { id: "duration", accessorFn: (r) => r.duration_ms ?? -1, header: "Duration", meta: { align: "right" }, cell: ({ row }) => formatMs(row.original.duration_ms) },
      { id: "rows_in", accessorFn: (r) => r.rows_in ?? -1, header: "Rows in", meta: { align: "right", hideOnMobile: true }, cell: ({ row }) => formatNumber(row.original.rows_in) },
      { id: "rows_out", accessorFn: (r) => r.rows_out ?? -1, header: "Rows out", meta: { align: "right" }, cell: ({ row }) => formatNumber(row.original.rows_out) },
      {
        id: "rows_rejected",
        accessorFn: (r) => r.rows_rejected ?? -1,
        header: "Rejected",
        meta: { align: "right", hideOnMobile: true },
        cell: ({ row }) => <span className={cn((row.original.rows_rejected ?? 0) > 0 && "text-warning")}>{formatNumber(row.original.rows_rejected)}</span>,
      },
      {
        id: "quality",
        accessorFn: (r) => r.quality_summary?.score ?? -1,
        header: "Quality",
        meta: { align: "right", hideOnMobile: true },
        cell: ({ row }) => (row.original.quality_summary?.score !== null && row.original.quality_summary?.score !== undefined ? Math.round(row.original.quality_summary.score) : "—"),
      },
      {
        id: "error",
        accessorFn: (r) => r.error_code ?? "",
        header: "Error",
        meta: { hideOnMobile: true },
        cell: ({ row }) => (row.original.error_code ? <span className="font-mono text-xs text-danger">{row.original.error_code}</span> : null),
      },
    ],
    [],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/data/pipelines/${id}`} className="inline-flex items-center gap-1 normal-case tracking-normal hover:text-fg">
            <ArrowLeft className="size-3" /> {pipeline.data?.name ?? "Pipeline"}
          </Link>
        }
        title="Run history"
        description="Status, attempts, row counts and quality for every run. Live runs refresh automatically."
        actions={
          <Button variant="secondary" asChild>
            <Link href={`/o/${slug}/data/pipelines/${id}`}>
              <Workflow /> Open builder
            </Link>
          </Button>
        }
      />
      {runs.isError && isUnavailableEndpoint(runs.error) ? (
        <Card>
          <EmptyState icon={<History />} title="Run history isn't available" description="The pipeline doesn't exist or the service isn't deployed yet." />
        </Card>
      ) : (
        <DataTable
          caption="Pipeline runs"
          columns={columns}
          data={runs.data}
          loading={runs.isLoading}
          error={runs.isError ? runs.error : undefined}
          onRetry={() => void runs.refetch()}
          onRowClick={(r) => router.push(`/o/${slug}/data/pipelines/${id}/runs/${r.id}`)}
          getRowId={(r) => r.id}
          initialSorting={[{ id: "started_at", desc: true }]}
          filters={[{ columnId: "status", label: "Status", options: Array.from(new Set((runs.data ?? []).map((r) => r.status))).map((s) => ({ value: s, label: titleCase(s) })) }]}
          emptyState={
            <EmptyState
              compact
              icon={<History />}
              title="No runs yet"
              description="Run the pipeline from the builder or wait for its schedule."
              action={
                <Button size="sm" variant="secondary" asChild>
                  <Link href={`/o/${slug}/data/pipelines/${id}`}>
                    <Workflow /> Open builder
                  </Link>
                </Button>
              }
            />
          }
        />
      )}
    </div>
  );
}

/* ---------------- run detail ---------------- */

export function RunDetailView() {
  const { id, runId } = useParams<{ id: string; runId: string }>();
  const { slug, can } = useOrgContext();
  const router = useRouter();
  const q = usePipelineRun(slug, runId);
  const live = LIVE.has(q.data?.status ?? "");
  const logs = useRunLogs(slug, runId, live);
  const retry = useRetryRun(slug);
  useSetBreadcrumb(q.data ? `Run ${runId.slice(0, 8)}` : null);

  if (q.isLoading) return <Skeleton className="h-96 rounded-card" />;
  if (q.isError || !q.data) {
    return (
      <Card>
        {isUnavailableEndpoint(q.error) ? (
          <EmptyState icon={<History />} title="Run not found" action={<Button variant="secondary" asChild><Link href={`/o/${slug}/data/pipelines/${id}/runs`}>Back to runs</Link></Button>} />
        ) : (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        )}
      </Card>
    );
  }
  const r = q.data;
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={`/o/${slug}/data/pipelines/${id}/runs`} className="inline-flex items-center gap-1 normal-case tracking-normal hover:text-fg">
            <ArrowLeft className="size-3" /> Run history
          </Link>
        }
        title={`Run ${r.id.slice(0, 8)}`}
        badge={<StatusBadge status={r.status} />}
        description={`${titleCase(r.trigger ?? "manual")} · attempt ${r.attempt} · started ${formatDateTime(r.started_at ?? r.created_at)}`}
        actions={
          can("pipelines.run") && (r.status === "failed" || r.status === "cancelled") ? (
            <Button
              loading={retry.isPending}
              onClick={async () => {
                try {
                  const res = await retry.mutateAsync(r.id);
                  toast.success("Retry queued");
                  if (res.run_id) router.push(`/o/${slug}/data/pipelines/${id}/runs/${res.run_id}`);
                  else void q.refetch();
                } catch (e) {
                  toast.error(errorMessage(e));
                }
              }}
            >
              <RotateCw /> Retry run
            </Button>
          ) : undefined
        }
      />

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5" aria-label="Run metrics">
        <Metric icon={<Timer />} label="Duration" value={formatMs(r.duration_ms)} />
        <Metric icon={<Rows3 />} label="Rows in" value={formatNumber(r.rows_in)} />
        <Metric icon={<Rows3 />} label="Rows out" value={formatNumber(r.rows_out)} />
        <Metric icon={<TriangleAlert />} label="Rows rejected" value={formatNumber(r.rows_rejected)} tone={(r.rows_rejected ?? 0) > 0 ? "warning" : undefined} />
        <Card className="flex items-center gap-3 p-4">
          <ScoreRing score={r.quality_summary?.score ?? null} size={44} label="Output quality" />
          <div className="text-xs text-fg-muted">
            Quality
            {r.quality_summary && (
              <span className="block">
                {r.quality_summary.rules_passed ?? 0} passed · {r.quality_summary.rules_failed ?? 0} failed
              </span>
            )}
          </div>
        </Card>
      </section>

      {(r.error_code || r.error_message) && (
        <div role="alert" className="flex gap-3 rounded-card border border-danger/30 bg-danger-soft/60 px-4 py-3">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-fg">Run failed{r.error_code ? `: ${r.error_code}` : ""}</div>
            {r.error_message && <p className="whitespace-pre-wrap break-words text-sm text-fg-muted">{r.error_message}</p>}
          </div>
        </div>
      )}

      {live && r.job_id && (
        <Card>
          <CardContent className="pt-4">
            <JobProgress org={slug} jobId={r.job_id} title="Live progress" onDone={() => void q.refetch()} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader title="Steps" description="Timeline of each step with rows in and out" />
        <CardContent>
          <StepTimeline steps={r.steps ?? []} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader title="Logs" description={live ? "Refreshing while the run is active" : undefined} />
        <CardContent>
          {logs.isError ? (
            isUnavailableEndpoint(logs.error) ? (
              <p className="text-sm text-fg-muted">Logs aren&apos;t available for this run.</p>
            ) : (
              <ErrorState compact error={logs.error} onRetry={() => void logs.refetch()} />
            )
          ) : logs.isLoading ? (
            <Skeleton className="h-40" />
          ) : (logs.data ?? []).length === 0 ? (
            <p className="text-sm text-fg-muted">No log lines.</p>
          ) : (
            <ol className="scrollbar-thin max-h-[420px] overflow-y-auto rounded-control bg-surface-2 p-3 font-mono text-[12px] leading-5">
              {(logs.data ?? []).map((l, i) => (
                <li key={i} className={cn("grid grid-cols-[auto_auto_1fr] gap-3", l.level === "error" ? "text-danger" : l.level === "warning" ? "text-warning" : "text-fg-muted")}>
                  <span className="text-fg-subtle">{new Date(l.ts).toLocaleTimeString()}</span>
                  <span className="text-fg-subtle">{l.step ?? "run"}</span>
                  <span className="whitespace-pre-wrap break-words">{l.message}</span>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Metric({ icon, label, value, tone }: { icon: React.ReactNode; label: string; value: React.ReactNode; tone?: "warning" }) {
  return (
    <Card className="flex items-center gap-3 p-4">
      <span className={cn("flex size-9 items-center justify-center rounded-control bg-surface-2 text-fg-subtle [&_svg]:size-4", tone === "warning" && "bg-warning-soft text-warning")} aria-hidden>
        {icon}
      </span>
      <div>
        <div className="tabular text-md font-semibold text-fg">{value}</div>
        <div className="text-xs text-fg-muted">{label}</div>
      </div>
    </Card>
  );
}

const STEP_BAR: Record<string, string> = {
  succeeded: "bg-success",
  failed: "bg-danger",
  running: "bg-info animate-pulse",
  skipped: "bg-fg-subtle/40",
  pending: "bg-surface-3",
};

/** Gantt-like step timeline: bars positioned by start/finish relative to the run window. */
export function StepTimeline({ steps }: { steps: PipelineRunStep[] }) {
  if (steps.length === 0) return <p className="text-sm text-fg-muted">No step metrics recorded.</p>;
  const times = steps.flatMap((s) => [s.started_at, s.finished_at]).filter(Boolean).map((t) => new Date(t!).getTime());
  const start = times.length ? Math.min(...times) : 0;
  const end = times.length ? Math.max(...times, Date.now() * (steps.some((s) => s.status === "running") ? 1 : 0)) : 1;
  const span = Math.max(1, end - start);
  return (
    <ol className="space-y-2" aria-label="Step timeline">
      {steps.map((s) => {
        const a = s.started_at ? new Date(s.started_at).getTime() : null;
        const b = s.finished_at ? new Date(s.finished_at).getTime() : s.status === "running" ? Date.now() : null;
        const left = a !== null ? ((a - start) / span) * 100 : 0;
        const width = a !== null && b !== null ? Math.max(1.5, ((b - a) / span) * 100) : 0;
        const err = typeof s.error === "string" ? s.error : s.error?.message;
        return (
          <li key={s.id} className="grid gap-2 sm:grid-cols-[180px_minmax(0,1fr)_160px] sm:items-center">
            <div className="min-w-0">
              <div className="truncate font-mono text-xs text-fg">{s.id}</div>
              <div className="truncate text-[11px] text-fg-subtle">{s.type}</div>
            </div>
            <div className="relative h-6 rounded-control bg-surface-2">
              {width > 0 && (
                <div
                  className={cn("absolute inset-y-1 rounded-[3px]", STEP_BAR[s.status] ?? "bg-accent")}
                  style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                  title={`${s.status} · ${a !== null && b !== null ? formatMs(b - a) : "—"}`}
                />
              )}
            </div>
            <div className="flex items-center justify-between gap-2 text-xs sm:justify-end">
              <StatusBadge status={s.status} />
              <span className="tabular text-fg-muted">
                {formatNumber(s.rows_in)} → {formatNumber(s.rows_out)}
              </span>
            </div>
            {err && <p className="text-xs text-danger sm:col-span-3">{err}</p>}
          </li>
        );
      })}
      <li className="flex justify-between pt-1 text-[11px] text-fg-subtle sm:ml-[188px] sm:mr-[168px]">
        <span>0s</span>
        <Badge tone="outline">{formatMs(span)}</Badge>
      </li>
    </ol>
  );
}
