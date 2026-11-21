"use client";

import * as React from "react";
import { HeartPulse, ListChecks, RefreshCw, Workflow } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/feedback";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { healthTone } from "@/lib/platform";
import { usePlatformHealth, usePlatformJobs, type HealthComponent } from "@/lib/queries/platform";
import { formatDateTime, formatNumber, formatRelative, titleCase } from "@/lib/utils";
import { useHiddenGuard } from "./platform-console";

const label = (k: string) => titleCase(k.replace(/_/g, " "));

function scalar(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return formatNumber(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function ComponentCard({ name, c }: { name: string; c: HealthComponent }) {
  const extra = Object.entries(c).filter(([k]) => !["status", "latency_ms", "error", "detail"].includes(k));
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-fg">{label(name)}</span>
        <Badge tone={healthTone(c.status)}>{c.status}</Badge>
      </div>
      {typeof c.latency_ms === "number" && <div className="tabular mt-1 text-xs text-fg-muted">{c.latency_ms} ms</div>}
      {c.error && <div className="mt-1 font-mono text-xs text-danger">{c.error}</div>}
      {c.detail && <div className="mt-1 text-xs text-fg-muted">{c.detail}</div>}
      {extra.length > 0 && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          {extra.map(([k, v]) => (
            <React.Fragment key={k}>
              <dt className="text-fg-subtle">{label(k)}</dt>
              <dd className="truncate text-fg-muted" title={scalar(v)}>
                {scalar(v)}
              </dd>
            </React.Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}

export function HealthPanel({ onHidden }: { onHidden: () => void }) {
  const q = usePlatformHealth();
  useHiddenGuard(q.error, onHidden);
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (q.isLoading || !q.data) return <Skeleton className="h-64" />;
  const h = q.data;
  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          icon={<HeartPulse />}
          title="Service health"
          description={`Checked ${formatRelative(h.checked_at)} · refreshes every 30 s`}
          actions={
            <div className="flex items-center gap-2">
              <Badge tone={healthTone(h.status)}>Overall: {h.status}</Badge>
              <Button size="xs" variant="ghost" onClick={() => void q.refetch()} loading={q.isFetching} aria-label="Refresh health">
                <RefreshCw />
              </Button>
            </div>
          }
        />
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {Object.entries(h.components).map(([k, c]) => (
              <ComponentCard key={k} name={k} c={c} />
            ))}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader title="Runtime configuration" description="Effective runtime settings of this installation (no secrets)." />
        <CardContent>
          <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
            {Object.entries(h.runtime).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3 border-b border-border/60 py-1">
                <dt className="text-fg-muted">{label(k)}</dt>
                <dd className="truncate font-mono text-xs text-fg" title={scalar(v)}>
                  {scalar(v)}
                </dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  );
}

const JOB_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"] as const;
const jobTone = (s: string) => (s === "succeeded" ? "success" : s === "failed" ? "danger" : s === "running" ? "info" : s === "queued" ? "warning" : "neutral");

export function JobsPanel({ onHidden }: { onHidden: () => void }) {
  const [status, setStatus] = React.useState("all");
  const [queueText, setQueueText] = React.useState("");
  const [queue, setQueue] = React.useState("");
  React.useEffect(() => {
    const t = setTimeout(() => {
      const v = queueText.trim().toLowerCase();
      setQueue(/^[a-z0-9_.-]{1,60}$/.test(v) ? v : "");
    }, 300);
    return () => clearTimeout(t);
  }, [queueText]);
  const q = usePlatformJobs({ queue, status: status === "all" ? "" : status });
  useHiddenGuard(q.error, onHidden);
  const d = q.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <Input className="w-56" placeholder="Job type prefix (e.g. ml)" aria-label="Job type prefix" value={queueText} maxLength={60} onChange={(e) => setQueueText(e.target.value)} />
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-48" aria-label="Job status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Failures (last 24 h)</SelectItem>
            {JOB_STATUSES.map((s) => (
              <SelectItem key={s} value={s}>
                {titleCase(s)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.isLoading || !d ? (
        <Skeleton className="h-64" />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="Queue depth" value={formatNumber(d.queue.depth)} hint={d.queue.oldest_queued_at ? `oldest ${formatRelative(d.queue.oldest_queued_at)}` : "nothing waiting"} />
            <Stat label="Failures (24 h)" value={formatNumber(d.failures_24h)} tone={d.failures_24h ? "danger" : undefined} />
            {(["running", "succeeded"] as const).map((s) => (
              <Stat key={s} label={titleCase(s)} value={formatNumber(d.by_status[s] ?? 0)} />
            ))}
          </div>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader icon={<Workflow />} title="Queued and running by type" />
              <CardContent>
                {Object.keys(d.queue.by_type).length === 0 ? (
                  <p className="text-sm text-fg-muted">The queue is empty.</p>
                ) : (
                  <ul className="space-y-1 text-sm">
                    {Object.entries(d.queue.by_type).map(([t, n]) => (
                      <li key={t} className="flex justify-between gap-3">
                        <span className="font-mono text-xs">{t}</span>
                        <span className="tabular">{formatNumber(n)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </CardContent>
            </Card>
            <Card>
              <CardHeader title="Task runner" description="Status of the in-process worker pool." />
              <CardContent>
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  {Object.entries(d.runner).map(([k, v]) => (
                    <React.Fragment key={k}>
                      <dt className="text-fg-muted">{label(k)}</dt>
                      <dd className="font-mono text-xs text-fg">{scalar(v)}</dd>
                    </React.Fragment>
                  ))}
                </dl>
              </CardContent>
            </Card>
          </div>
          <Card>
            <CardHeader icon={<ListChecks />} title={status === "all" ? "Failed jobs (last 24 h)" : `${titleCase(status)} jobs`} description="Error codes only; job payloads are tenant data and are not shown." />
            <CardContent>
              {d.items.length === 0 ? (
                <EmptyState compact icon={<ListChecks />} title="No jobs" />
              ) : (
                <div className="scrollbar-thin overflow-x-auto">
                  <table className="w-full min-w-[720px] text-sm">
                    <caption className="sr-only">Jobs</caption>
                    <thead className="text-xs text-fg-muted">
                      <tr className="border-b border-border">
                        <th className="py-2 pr-3 text-left font-medium">Type</th>
                        <th className="py-2 pr-3 text-left font-medium">Organization</th>
                        <th className="py-2 pr-3 text-left font-medium">Status</th>
                        <th className="py-2 pr-3 text-left font-medium">Error</th>
                        <th className="py-2 pr-3 text-left font-medium">Created</th>
                        <th className="py-2 text-left font-medium">Finished</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.items.map((j) => (
                        <tr key={j.id} className="border-b border-border/60 last:border-0">
                          <td className="py-1.5 pr-3 font-mono text-xs">{j.type}</td>
                          <td className="py-1.5 pr-3 font-mono text-xs">{j.org_slug ?? j.org_id.slice(0, 8)}</td>
                          <td className="py-1.5 pr-3">
                            <Badge tone={jobTone(j.status)}>{j.status}</Badge>
                          </td>
                          <td className="py-1.5 pr-3 font-mono text-xs text-danger">{j.error_code ?? ""}</td>
                          <td className="py-1.5 pr-3 text-xs text-fg-muted" title={formatDateTime(j.created_at)}>
                            {formatRelative(j.created_at)}
                          </td>
                          <td className="py-1.5 text-xs text-fg-muted">{j.finished_at ? formatDateTime(j.finished_at) : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

export function Stat({ label: l, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "danger" }) {
  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="text-xs text-fg-muted">{l}</div>
      <div className={`tabular mt-1 text-2xl font-semibold ${tone === "danger" ? "text-danger" : "text-fg"}`}>{value}</div>
      {hint && <div className="mt-0.5 text-xs text-fg-subtle">{hint}</div>}
    </div>
  );
}
