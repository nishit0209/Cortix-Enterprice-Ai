"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Hand, ListTree } from "lucide-react";
import { useOrgContext, useSetBreadcrumb } from "@/components/shell/org-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { PageHeader } from "@/components/ui/page-header";
import { StatusBadge } from "@/components/ui/status";
import type { StepRun } from "@/lib/automation/graph";
import { useRunSteps, useWorkflowRun } from "@/lib/queries/automation";
import { cn, formatDateTime, formatRelative, titleCase } from "@/lib/utils";
import { ModuleError } from "@/components/mdm/common";

const LIVE = new Set(["queued", "running", "waiting_approval", "retrying"]);

export function duration(a: string | null, b: string | null): string {
  if (!a || !b) return "";
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function RunView() {
  const { id } = useParams<{ id: string }>();
  const { slug } = useOrgContext();
  const run = useWorkflowRun(slug, id);
  const live = !!run.data && LIVE.has(run.data.status);
  const steps = useRunSteps(slug, id, live);
  useSetBreadcrumb(run.data ? `Run ${formatDateTime(run.data.created_at)}` : null);
  if (run.isError) {
    return (
      <Card>
        <ModuleError error={run.error} what="This run" onRetry={() => void run.refetch()} />
      </Card>
    );
  }
  const r = run.data;
  if (!r) return <Skeleton className="h-96 rounded-card" />;
  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={
          <Link href={r.workflow_id ? `/o/${slug}/automation/workflows/${r.workflow_id}` : `/o/${slug}/automation?tab=runs`} className="inline-flex items-center gap-1 hover:underline">
            <ArrowLeft className="size-3" /> {r.workflow_name ?? "Automation"}
          </Link>
        }
        title={`Run of ${r.workflow_name ?? "workflow"}${r.version ? ` v${r.version}` : ""}`}
        badge={<StatusBadge status={r.status} />}
        description={`${r.trigger_type ? `${titleCase(r.trigger_type)} trigger · ` : ""}started ${formatDateTime(r.started_at ?? r.created_at)}${r.finished_at ? ` · took ${duration(r.started_at, r.finished_at)}` : ""}`}
      />
      {r.status === "waiting_approval" && (
        <Card className="flex flex-wrap items-center justify-between gap-3 border-warning/40 bg-warning-soft p-4">
          <p className="flex items-center gap-2 text-sm">
            <Hand className="size-4" /> This run is paused until its approval step is decided.
          </p>
          <Button size="sm" asChild>
            <Link href={`/o/${slug}/automation?tab=approvals`}>Open approvals</Link>
          </Button>
        </Card>
      )}
      {r.error && <p className="rounded-control border border-danger/30 bg-danger-soft px-3 py-2 text-sm text-danger">{r.error}</p>}
      <Card>
        <CardHeader title="Step timeline" description={live ? "Updates every few seconds while the run is active." : undefined} />
        <CardContent>
          {steps.isError ? (
            <ModuleError compact error={steps.error} what="Step runs" onRetry={() => void steps.refetch()} />
          ) : steps.isLoading ? (
            <Skeleton className="h-40" />
          ) : !(steps.data ?? []).length ? (
            <EmptyState compact icon={<ListTree />} title="No steps have run yet" />
          ) : (
            <ol className="relative space-y-3 border-l border-border pl-5">
              {steps.data!.map((s) => (
                <StepItem key={s.id} step={s} />
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function StepItem({ step }: { step: StepRun }) {
  const [open, setOpen] = React.useState(false);
  const tone = step.status === "succeeded" ? "bg-success" : step.status === "failed" ? "bg-danger" : LIVE.has(step.status) ? "bg-info" : "bg-fg-subtle";
  const hasOutput = step.output !== null && step.output !== undefined && !(typeof step.output === "object" && Object.keys(step.output as object).length === 0);
  return (
    <li className="relative">
      <span className={cn("absolute top-1.5 -left-[25px] size-2.5 rounded-full border-2 border-surface", tone)} aria-hidden />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{step.step_key}</span>
        {step.kind && <Badge tone="outline">{step.kind}</Badge>}
        <StatusBadge status={step.status} />
        {step.attempt > 1 && <span className="text-xs text-warning">attempt {step.attempt}</span>}
        <span className="text-xs text-fg-subtle">
          {step.started_at ? formatRelative(step.started_at) : ""}
          {step.finished_at ? ` · ${duration(step.started_at, step.finished_at)}` : ""}
        </span>
        {hasOutput && (
          <button type="button" className="text-xs text-accent hover:underline" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {open ? "Hide output" : "Output"}
          </button>
        )}
      </div>
      {step.error && <p className="mt-1 text-xs text-danger">{step.error}</p>}
      {open && hasOutput && <pre className="mt-2 max-h-64 overflow-auto rounded-control bg-surface-2 p-2 text-[11px]">{JSON.stringify(step.output, null, 2)}</pre>}
    </li>
  );
}
